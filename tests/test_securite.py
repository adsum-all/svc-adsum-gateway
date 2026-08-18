"""Les correctifs de l'audit de sécurité, chacun avec le défaut qu'il ferme.

Un correctif sans test se défait au premier remaniement, et personne ne le voit
puisque rien ne casse visiblement : la protection disparaît en silence. Chaque cas
ci-dessous reproduit la séquence exacte relevée en audit.
"""
from __future__ import annotations

import os

import pytest
from conftest import FournisseurObserve

from passerelle.adaptateurs.brevo import Brevo, _une_ligne
from passerelle.observabilite import Mesures, _route
from passerelle.port import Canal, Destinataire, Message


class TestEnTeteDeCourriel:
    """Une valeur libre qui devient un en-tête SMTP chez le fournisseur.

    Un retour à la ligne y ouvre une ligne d'en-tête supplémentaire : un « Bcc »
    choisi par l'appelant, par exemple, qui reçoit alors copie de tout.
    """

    def test_les_sauts_de_ligne_sont_retires(self):
        injecte = "CMD-1" + chr(13) + chr(10) + "Bcc: espion@ailleurs.test"
        propre = _une_ligne(injecte)
        assert chr(13) not in propre
        assert chr(10) not in propre
        assert "Bcc" in propre, "Le texte reste, seule la structure est neutralisée"

    def test_le_caractere_nul_est_retire(self):
        assert chr(0) not in _une_ligne("CMD" + chr(0) + "-1")

    def test_une_valeur_ordinaire_est_intacte(self):
        assert _une_ligne("CMD-2026-000123") == "CMD-2026-000123"

    def test_la_cle_et_l_objet_partent_assainis(self):
        """Les deux champs que Brevo recopie en en-tête."""
        class TransportObserve:
            def __init__(self):
                self.charge = None

            def envoyer(self, methode, url, entetes, corps=None, delai=30.0):
                import json

                self.charge = json.loads(corps.decode())

                class R:
                    code = 201
                    corps = b'{"messageId": "m-1"}'

                    @staticmethod
                    def json():
                        return {"messageId": "m-1"}
                return R()

        t = TransportObserve()
        Brevo("cle", "editeur@adsum.test", "ADSUM", transport=t).envoyer(Message(
            canal=Canal.COURRIEL,
            destinataire=Destinataire(adresse="client@exemple.test"),
            objet="Facture" + chr(10) + "Bcc: espion@ailleurs.test",
            corps="Bonjour",
            cle_idempotence="CMD-1" + chr(10) + "X-Injecte: oui",
        ))
        assert chr(10) not in t.charge["subject"]
        assert chr(10) not in t.charge["headers"]["X-ADSUM-Cle"]


class TestGardeAscii:
    """Un octet non ASCII faisait lever TypeError au lieu d'un refus.

    Conséquence : un 500 tracé au niveau erreur, là où il fallait un 401 tracé en
    avertissement. Un appelant non authentifié pilotait donc le niveau d'alerte du
    service avec un seul caractère.
    """

    def test_un_entete_non_ascii_est_refuse_proprement(self):
        from passerelle.port import ErreurEnvoi

        brevo = Brevo("cle", "editeur@adsum.test", "ADSUM", secret_accuse="secret")
        with pytest.raises(ErreurEnvoi):
            brevo.verifier_accuse({"x-adsum-accuse": "sécret"}, b"{}")

    def test_le_bon_secret_passe_toujours(self):
        brevo = Brevo("cle", "editeur@adsum.test", "ADSUM", secret_accuse="secret")
        accuse = brevo.verifier_accuse(
            {"x-adsum-accuse": "secret"},
            b'{"event": "delivered", "message-id": "m-1", "id": "e-1"}')
        assert accuse.reference == "m-1"


class TestMesuresBornees:
    """Deux compteurs sur trois n'étaient pas plafonnés.

    Un appelant non authentifié faisait croître la mémoire sans fin en variant
    l'URL, et la sortie des mesures avec elle.
    """

    def test_les_trois_structures_sont_bornees(self):
        m = Mesures(plafond_cles=10)
        for n in range(500):
            m.observer(f"/inconnu/{n}", "GET", 404, 0.01)
        assert len(m.requetes) <= 12
        assert len(m.somme_s) <= 12
        assert len(m.durees) <= 12 * len(
            __import__("passerelle.observabilite", fromlist=["PALIERS_S"]).PALIERS_S)

    def test_le_trafic_excedentaire_est_compte_sous_une_cle_unique(self):
        """Cesser de compter ferait geler les mesures en silence, ce qui est pire
        qu'une mesure grossière."""
        m = Mesures(plafond_cles=3)
        for n in range(50):
            m.observer(f"/x{n}", "GET", 404, 0.01)
        assert any(cle[0] == m.AUTRE for cle in m.requetes)
        assert sum(m.requetes.values()) == 50, "Aucun passage n'est perdu"

    def test_les_compteurs_connus_continuent_apres_le_plafond(self):
        """Le test portait sur la taille avant l'existence de la clé : une fois le
        plafond atteint, même les routes connues cessaient d'être comptées."""
        m = Mesures(plafond_cles=2)
        m.observer("/connue", "GET", 200, 0.01)
        for n in range(20):
            m.observer(f"/inconnue{n}", "GET", 404, 0.01)
        m.observer("/connue", "GET", 200, 0.01)
        assert m.requetes[("/connue", "GET", 200)] == 2

    def test_un_chemin_inconnu_ne_devient_pas_une_etiquette(self):
        """Le chemin brut est piloté de l'extérieur : dans une mesure conservée des
        mois, c'est une cardinalité sans limite et une fuite de ce qui a été tenté."""
        assert _route({"path": "/tentative/../../etc/passwd"}) == "/inconnu"
        assert _route({"path": "/api/v1/portail/commandes/CMD-2026-000123"}) == "/inconnu"


class TestPoivre:
    """Un condensé nu d'adresse ne protège pas contre une copie de la base.

    L'espace des adresses plausibles est petit : un dictionnaire les retrouve. Le
    poivre, gardé hors de la base, est ce qui rend la colonne inexploitable seule.
    """

    def test_le_service_refuse_de_condenser_sans_poivre(self, monkeypatch):
        import passerelle.journal as journal

        monkeypatch.delenv("ADSUM_PASSERELLE_POIVRE", raising=False)
        monkeypatch.setattr(journal, "_POIVRE", None)
        with pytest.raises(RuntimeError) as e:
            journal._empreinte("client@exemple.test")
        assert "POIVRE" in str(e.value)

    def test_un_poivre_trop_court_est_refuse(self, monkeypatch):
        """Un poivre de huit caractères ne vaut pas mieux que pas de poivre, et il
        donnerait l'illusion d'être protégé."""
        import passerelle.journal as journal

        monkeypatch.setenv("ADSUM_PASSERELLE_POIVRE", "trop-court")
        monkeypatch.setattr(journal, "_POIVRE", None)
        with pytest.raises(RuntimeError):
            journal._empreinte("client@exemple.test")

    def test_deux_poivres_donnent_deux_empreintes(self, monkeypatch):
        import passerelle.journal as journal

        monkeypatch.setenv("ADSUM_PASSERELLE_POIVRE", "a" * 40)
        monkeypatch.setattr(journal, "_POIVRE", None)
        premiere = journal._empreinte("client@exemple.test")

        monkeypatch.setenv("ADSUM_PASSERELLE_POIVRE", "b" * 40)
        monkeypatch.setattr(journal, "_POIVRE", None)
        seconde = journal._empreinte("client@exemple.test")

        assert premiere != seconde
        assert premiere.startswith("v1:"), "La version prépare la rotation du poivre"

    def test_la_casse_ne_change_pas_l_empreinte(self, monkeypatch):
        import passerelle.journal as journal

        monkeypatch.setenv("ADSUM_PASSERELLE_POIVRE", "c" * 40)
        monkeypatch.setattr(journal, "_POIVRE", None)
        assert journal._empreinte("Client@Exemple.TEST") == \
            journal._empreinte("client@exemple.test")


class TestExpurgationDuMotif:
    """Le motif du fournisseur nomme presque toujours l'adresse rejetée.

    Il était écrit dans trois tables et rendu dans une réponse HTTP, ce qui
    reconstituait dans le registre ce que le condensé existe pour cacher.
    """

    def test_l_adresse_disparait_du_motif(self):
        from passerelle.journal import sans_adresse

        propre = sans_adresse(
            "to[0].email is invalid: client@exemple.test", "client@exemple.test")
        assert "client@exemple.test" not in propre
        assert "[adresse]" in propre

    def test_une_adresse_voisine_disparait_aussi(self):
        """Un motif de rebond nomme parfois une autre adresse, celle d'un renvoi."""
        from passerelle.journal import sans_adresse

        propre = sans_adresse("forwarded to postmaster@ailleurs.test", "")
        assert "postmaster@ailleurs.test" not in propre

    def test_le_motif_est_tronque_avant_d_etre_parcouru(self):
        """L'expression régulière est quadratique et le motif vient d'un corps que
        l'appelant contrôle : sans troncature préalable, quelques mégaoctets
        occupent le processeur de l'instance."""
        from passerelle.journal import sans_adresse

        enorme = "a" * 200_000 + "@" + "b" * 200_000
        propre = sans_adresse(enorme, "")
        assert len(propre) <= 500


class TestMigrationCloisonnee:
    """La migration posait quatre tables sans schéma dédié.

    Appliquée avec le chemin de recherche par défaut, elle les plaçait dans
    « public », que l'hébergement expose par une API automatique accessible à un
    rôle anonyme.
    """

    def test_la_migration_ferme_le_schema_courant(self):
        """Le nom du schéma appartient au script d'application, pas à la migration.

        Le coder en dur ici empêcherait d'appliquer la migration dans un schéma de
        test ou dans un second environnement. Ce qui doit être dans la migration,
        c'est la fermeture des accès, quel que soit le schéma où elle est posée.
        """
        from pathlib import Path

        sql = Path(__file__).resolve().parents[1] / (
            "migrations/versions/0001_journal_envois.sql")
        texte = sql.read_text(encoding="utf-8")
        assert "current_schema()" in texte
        assert "REVOKE ALL ON SCHEMA %I FROM PUBLIC" in texte
        assert "CREATE SCHEMA" not in texte, "Le nom du schéma n'appartient pas ici"
        # « FORCE » et pas seulement « ENABLE » : la connexion applicative est
        # propriétaire du schéma, et un propriétaire contourne la sécurité au niveau
        # des lignes tant qu'elle n'est pas forcée.
        assert texte.count("FORCE  ROW LEVEL SECURITY") == 4

    def test_le_script_d_application_pose_le_schema_avant_les_migrations(self):
        """C'est lui qui empêche les tables d'atterrir dans « public », où
        l'hébergement les exposerait sans authentification."""
        from pathlib import Path

        script = Path(__file__).resolve().parents[3] / (
            "deployment/database/creer_schema_passerelle.py")
        assert script.exists(), "Le script d'application manque"
        texte = script.read_text(encoding="utf-8")
        assert 'CREATE SCHEMA IF NOT EXISTS {SCHEMA}' in texte
        assert 'SET search_path TO {SCHEMA}' in texte
        # Il vérifie après coup que rien n'a bougé dans « public ».
        assert 'compter(cur, "public") != avant_public' in texte


class TestPlancherDeSecret:
    def test_un_secret_court_empeche_le_demarrage(self, monkeypatch):
        """Un secret de huit caractères se devine, et il ouvre l'envoi de messages
        sous l'identité de l'éditeur."""
        from passerelle.application import Configuration

        monkeypatch.setenv("ADSUM_PASSERELLE_DSN", "postgresql://x")
        monkeypatch.setenv("ADSUM_PASSERELLE_SECRET", "court")
        with pytest.raises(RuntimeError) as e:
            Configuration.depuis_environnement()
        assert "32" in str(e.value)

    def test_un_secret_suffisant_passe(self, monkeypatch):
        from passerelle.application import Configuration

        monkeypatch.setenv("ADSUM_PASSERELLE_DSN", "postgresql://x")
        monkeypatch.setenv("ADSUM_PASSERELLE_SECRET", "z" * 40)
        assert Configuration.depuis_environnement().secret_appel == "z" * 40


def test_aucun_secret_ne_traine_dans_l_environnement_du_test():
    """Garde-fou : ces tests posent des secrets, ils ne doivent pas fuiter ailleurs."""
    assert not os.environ.get("ADSUM_BREVO_CLE")


class TestSecretsDansLUrl:
    """Certains fournisseurs portent leur identifiant dans l'adresse.

    Un robot Telegram s'appelle « /bot<jeton>/sendMessage ». Le transport tracé se
    décrit comme n'écrivant jamais de secret, et masquait pourtant les seuls
    en-têtes : le jeton partait en clair dans les traces.
    """

    def test_le_jeton_du_robot_est_masque(self):
        from passerelle.transport import masquer_url

        propre = masquer_url("https://api.telegram.org/bot123456:SECRET/sendMessage")
        assert "SECRET" not in propre
        assert propre.endswith("/sendMessage"), "Le reste doit rester lisible"

    def test_la_chaine_de_requete_est_retiree(self):
        """Les fournisseurs sans en-tête d'autorisation y mettent leur clé."""
        propre = __import__(
            "passerelle.transport", fromlist=["masquer_url"]).masquer_url(
                "https://api.exemple.test/v1/paiement?apikey=SECRET&id=1")
        assert "SECRET" not in propre
        assert propre == "https://api.exemple.test/v1/paiement"

    def test_une_adresse_ordinaire_est_intacte(self):
        from passerelle.transport import masquer_url

        assert masquer_url("https://api.brevo.com/v3/smtp/email") == \
            "https://api.brevo.com/v3/smtp/email"

    def test_l_entete_de_cle_brevo_est_masque(self):
        """« api-key » avec le tiret : la graphie exacte que Brevo attend, et celle
        qui manquait à la liste des en-têtes masqués."""
        from passerelle.transport import masquer

        masques = masquer({"api-key": "xkeysib-SECRET", "accept": "application/json"})
        assert masques["api-key"] == "***"
        assert masques["accept"] == "application/json"


class TestConnexionFermee:
    """La connexion n'était fermée qu'au ramassage des objets, c'est-à-dire jamais
    sur un chemin où une exception garde la trame vivante, ni sur un hébergeur qui
    gèle le processus après la réponse. Les connexions s'accumulaient jusqu'à
    épuiser la réserve du répartiteur, et la panne se manifestait ailleurs."""

    def test_la_connexion_est_fermee_meme_quand_la_route_echoue(self):
        from fastapi.testclient import TestClient

        from passerelle.application import Configuration, creer_application
        from passerelle.service import Registre

        fermees = []

        class ConnexionObservee:
            def cursor(self):
                raise RuntimeError("base en panne")

            def close(self):
                fermees.append(True)

        client = TestClient(
            creer_application(
                Configuration(dsn="", secret_appel="s" * 40),
                ouvrir_connexion=ConnexionObservee, registre=Registre()),
            raise_server_exceptions=False)

        client.get("/api/v1/envois/CLE-X",
                   headers={"Authorization": "Bearer " + "s" * 40})
        assert fermees, "La connexion doit être fermée même sur une panne"


class TestConservation:
    """Le registre accumulait indéfiniment.

    Conserver sans limite une donnée personnelle, fût-elle condensée, contrevient au
    principe de limitation de la conservation. Une durée doit être définie, et
    surtout appliquée : une durée qui n'existe que dans une politique écrite n'est
    pas une durée.
    """

    def test_les_durees_sont_declarees(self):
        from passerelle.journal import (
            CONSERVATION_ENVOIS_J,
            CONSERVATION_TENTATIVES_J,
        )

        # Treize mois : de quoi couvrir un exercice comptable et la réclamation qui
        # arrive après. Les tentatives ne servent qu'à comprendre une panne en cours.
        assert CONSERVATION_ENVOIS_J == 395
        assert CONSERVATION_TENTATIVES_J == 90
        assert CONSERVATION_TENTATIVES_J < CONSERVATION_ENVOIS_J

    def test_la_purge_efface_le_perime_et_garde_le_reste(self, base):
        from passerelle.journal import Journal

        with base() as conn:
            journal = Journal(conn)
            with journal.transaction() as cur:
                cur.execute(
                    "INSERT INTO envoi (cle_idempotence, canal, adresse_empreinte, "
                    "  statut, envoye_le, cree_le) VALUES "
                    "('VIEUX', 'courriel', 'v1:abc', 'remis', "
                    "   now() - interval '400 days', now() - interval '400 days'), "
                    "('RECENT', 'courriel', 'v1:def', 'remis', now(), now())")
                cur.execute(
                    "INSERT INTO tentative (cle_idempotence, fournisseur, resultat, "
                    "  faite_le) VALUES "
                    "('RECENT', 'brevo', 'accepte', now() - interval '100 days'), "
                    "('RECENT', 'brevo', 'accepte', now())")

            with journal.transaction() as cur:
                compte = journal.purger(cur, 395, 90)

            with journal.transaction() as cur:
                cur.execute("SELECT cle_idempotence FROM envoi ORDER BY 1")
                restants = [r["cle_idempotence"] for r in cur.fetchall()]
                cur.execute("SELECT count(*) AS n FROM tentative")
                tentatives = cur.fetchone()["n"]

        assert compte["envois"] >= 1
        assert "VIEUX" not in restants, "L'envoi périmé doit partir"
        assert "RECENT" in restants, "L'envoi récent doit rester"
        assert tentatives == 1, "Seule la tentative périmée part"

    def test_un_blocage_n_est_jamais_purge(self, base):
        """Un désabonnement est définitif. L'oublier ferait repartir des messages
        vers quelqu'un qui a demandé à ne plus en recevoir."""
        from passerelle.journal import Canal, Journal

        with base() as conn:
            journal = Journal(conn)
            with journal.transaction() as cur:
                journal.bloquer(cur, Canal.COURRIEL, "jamais@exemple.test", "désabonné")
                cur.execute(
                    "UPDATE adresse_bloquee SET bloquee_le = now() - interval '5 years'")
            with journal.transaction() as cur:
                journal.purger(cur, 1, 1)
            with journal.transaction() as cur:
                assert journal.est_bloquee(
                    cur, Canal.COURRIEL, "jamais@exemple.test") is not None


class TestSecretParAppelant:
    """Un secret unique pour tous ne dit pas qui a demandé un envoi.

    Avec lui, un service compromis écrit à n'importe quelle adresse sous l'identité
    de l'éditeur, et rien dans le registre ne permet de dire lequel, ni de le
    révoquer sans couper tous les autres.
    """

    @staticmethod
    def _client(base, **config):
        from fastapi.testclient import TestClient

        from passerelle.application import Configuration, creer_application
        from passerelle.service import Registre

        registre = Registre()
        registre.enregistrer(FournisseurObserve("premier"))
        return TestClient(
            creer_application(Configuration(dsn="", **config),
                              ouvrir_connexion=base, registre=registre),
            raise_server_exceptions=False)

    def test_le_registre_retient_qui_a_demande_l_envoi(self, base):
        secret_commerce = "c" * 40
        client = self._client(
            base, secret_appel="s" * 40,
            secrets_nommes=f"commerce:{secret_commerce},ouvriers:{'o' * 40}")

        reponse = client.post(
            "/api/v1/envois",
            json={"canal": "courriel", "adresse": "a@b.test", "objet": "O",
                  "corps": "C", "cle_idempotence": "CLE-APPELANT"},
            headers={"Authorization": f"Bearer {secret_commerce}"})
        assert reponse.status_code == 200

        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT appelant FROM envoi "
                        "WHERE cle_idempotence = 'CLE-APPELANT'")
            assert cur.fetchone()["appelant"] == "commerce"

    def test_le_secret_historique_reste_accepte(self, base):
        """Le retirer le jour où l'on pose les secrets nommés couperait les services
        déjà en place, et la panne se verrait par des relances qui ne partent plus."""
        client = self._client(base, secret_appel="h" * 40,
                              secrets_nommes=f"commerce:{'c' * 40}")
        reponse = client.post(
            "/api/v1/envois",
            json={"canal": "courriel", "adresse": "a@b.test", "objet": "O",
                  "corps": "C", "cle_idempotence": "CLE-HISTORIQUE"},
            headers={"Authorization": "Bearer " + "h" * 40})
        assert reponse.status_code == 200

        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT appelant FROM envoi "
                        "WHERE cle_idempotence = 'CLE-HISTORIQUE'")
            assert cur.fetchone()["appelant"] == "inconnu"

    def test_un_secret_revoque_ne_passe_plus(self, base):
        """C'est tout l'intérêt : couper un appelant sans toucher aux autres."""
        client = self._client(base, secret_appel="h" * 40,
                              secrets_nommes=f"commerce:{'c' * 40}")
        reponse = client.post(
            "/api/v1/envois",
            json={"canal": "courriel", "adresse": "a@b.test", "objet": "O",
                  "corps": "C", "cle_idempotence": "CLE-REVOQUE"},
            headers={"Authorization": "Bearer " + "r" * 40})
        assert reponse.status_code == 401

    def test_un_secret_nomme_trop_court_est_ecarte(self, base):
        """La variable s'édite à la main : une faute de frappe y produirait sinon un
        appelant qui n'entre jamais, sans que rien ne dise pourquoi."""
        client = self._client(base, secret_appel="h" * 40,
                              secrets_nommes="fragile:court")
        reponse = client.post(
            "/api/v1/envois",
            json={"canal": "courriel", "adresse": "a@b.test", "objet": "O",
                  "corps": "C", "cle_idempotence": "CLE-COURT"},
            headers={"Authorization": "Bearer court"})
        assert reponse.status_code == 401

    def test_la_migration_pose_la_colonne_et_son_index(self):
        from pathlib import Path

        sql = Path(__file__).resolve().parents[1] / (
            "migrations/versions/0002_appelant.sql")
        texte = sql.read_text(encoding="utf-8")
        assert "ADD COLUMN appelant" in texte
        assert "DEFAULT 'inconnu'" in texte, "Les lignes existantes doivent rester valides"
        assert "CREATE INDEX envoi_par_appelant" in texte
