"""La passerelle : ce qui part, ce qui ne repart pas, et ce qui ne part jamais.

Vraie application HTTP, vraie base. Seuls les fournisseurs sont observés plutôt
qu'appelés : envoyer de vrais courriels depuis une suite de tests remplirait des
boîtes réelles et abîmerait la réputation du domaine d'expédition, ce qui est
exactement le dommage que ce service existe pour éviter.

Ce qui est éprouvé, par ordre de coût quand cela casse.

Une mise en demeure ne part pas deux fois. C'est la raison d'être du journal, et le
seul défaut de cette classe qui se paie en confiance client.

Une adresse bloquée ne reçoit plus rien, et le blocage survit au redémarrage. Écrire
à une adresse morte abîme la réputation d'envoi du domaine, donc la remise de tous
les autres messages.

Un fournisseur en panne ne fait pas taire la plateforme : le suivant prend.

Et le registre ne porte jamais d'adresse en clair.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.parse
from pathlib import Path

import pytest

from passerelle.application import Configuration, creer_application
from passerelle.port import (
    Canal,
    DestinataireBloque,
    Envoi,
    ErreurEnvoi,
    Fournisseur,
    Message,
    Statut,
)
from passerelle.service import Registre

SCHEMA = "test_passerelle"
SECRET_BASE = Path("C:/Users/kouas/Documents/deepl-test/95-sr-adsum/.secret/supabase-secret-adsum.json")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
SECRET_APPEL = "secret-de-test-passerelle-jamais-en-production"
ENTETES = {"Authorization": f"Bearer {SECRET_APPEL}"}


def _dsn() -> str | None:
    if not SECRET_BASE.exists():
        return None
    s = json.load(io.open(SECRET_BASE, encoding="utf-8"))["supabase"]
    if not s.get("db_password"):
        return None
    mdp = urllib.parse.quote(s["db_password"], safe="")
    return (f"postgresql://postgres.{s['project_id']}:{mdp}"
            f"@aws-0-{s['region']}.pooler.supabase.com:5432/postgres?sslmode=require")


@pytest.fixture(scope="module")
def base():
    dsn = os.environ.get("PASSERELLE_DSN") or _dsn()
    if not dsn:
        pytest.skip("Aucune base joignable")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg absent")
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Base injoignable : {type(e).__name__}")

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        for fichier in sorted(MIGRATIONS.glob("*.sql")):
            cur.execute(io.open(fichier, encoding="utf-8").read())
    conn.commit()
    conn.close()

    def ouvrir():
        c = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
        c.commit()
        return c

    yield ouvrir

    fin = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
    with fin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    fin.commit()
    fin.close()


class FournisseurObserve(Fournisseur):
    """Note ce qu'on lui demande, répond ce qu'on lui a dit de répondre.

    Pas une doublure du domaine : les vrais adaptateurs sont éprouvés ailleurs
    contre leurs propres formats. Celui-ci permet de provoquer une panne de
    fournisseur, ce qu'aucun service réel ne fait sur commande.
    """

    canaux = (Canal.COURRIEL,)

    def __init__(self, code: str = "observe", panne: str | None = None,
                 bloque: bool = False) -> None:
        self.code = code
        self.libelle = code
        self.panne = panne
        self.bloque = bloque
        self.envois: list[Message] = []

    def envoyer(self, message: Message) -> Envoi:
        self.envois.append(message)
        if self.bloque:
            raise DestinataireBloque("Adresse morte")
        if self.panne:
            raise ErreurEnvoi(self.panne)
        return Envoi(
            cle_idempotence=message.cle_idempotence,
            statut=Statut.ACCEPTE,
            fournisseur=self.code,
            canal=message.canal,
            reference=f"REF-{len(self.envois)}-{self.code}",
            envoye_le=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc),
        )


@pytest.fixture()
def premier() -> FournisseurObserve:
    return FournisseurObserve("premier")


@pytest.fixture()
def client_http(base, premier):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi absent")
    registre = Registre()
    registre.enregistrer(premier, rang=10)
    config = Configuration(dsn="", secret_appel=SECRET_APPEL)
    return TestClient(
        creer_application(config, ouvrir_connexion=base, registre=registre),
        raise_server_exceptions=False)


def demande(cle: str, adresse: str = "tresorier@paroisse.test", **extra) -> dict:
    return {
        "canal": "courriel",
        "adresse": adresse,
        "objet": "Facture F-2026-0042 en retard",
        "corps": "Votre abonnement arrive à échéance.",
        "cle_idempotence": cle,
        "categorie": "relance",
        "organisation": "paroisse-saint-jean",
        **extra,
    }


class TestAuthentification:
    def test_sans_secret_rien_ne_part(self, client_http, premier):
        reponse = client_http.post("/api/v1/envois", json=demande("CLE-1"))
        assert reponse.status_code == 401
        assert premier.envois == [], "Aucun appel de fournisseur sans authentification"

    def test_un_secret_faux_est_refuse(self, client_http, premier):
        reponse = client_http.post(
            "/api/v1/envois", json=demande("CLE-2"),
            headers={"Authorization": "Bearer pas-le-bon"})
        assert reponse.status_code == 401
        assert premier.envois == []

    def test_la_sonde_reste_ouverte(self, client_http):
        """Une sonde qui exige un secret n'est pas une sonde."""
        reponse = client_http.get("/health")
        assert reponse.status_code == 200
        assert reponse.json()["canaux"] == ["courriel"]


class TestIdempotence:
    def test_une_mise_en_demeure_ne_part_pas_deux_fois(self, client_http, premier):
        """Le défaut le plus coûteux du service, et le seul qui se paie en confiance."""
        premiere = client_http.post("/api/v1/envois", json=demande("CLE-UNIQUE"),
                                    headers=ENTETES)
        seconde = client_http.post("/api/v1/envois", json=demande("CLE-UNIQUE"),
                                   headers=ENTETES)

        assert premiere.status_code == 200
        assert seconde.status_code == 200
        assert len(premier.envois) == 1, "Le fournisseur ne doit être appelé qu'une fois"
        assert seconde.json()["deja_fait"] is True
        assert seconde.json()["reference"] == premiere.json()["reference"]

    def test_deux_cles_differentes_partent_toutes_les_deux(self, client_http, premier):
        client_http.post("/api/v1/envois", json=demande("CLE-A"), headers=ENTETES)
        client_http.post("/api/v1/envois", json=demande("CLE-B"), headers=ENTETES)
        assert len(premier.envois) == 2

    def test_une_reservation_est_prise_avant_l_appel(self, client_http, base, premier):
        """L'ordre compte : réserver après l'envoi laisse la fenêtre du doublon."""
        client_http.post("/api/v1/envois", json=demande("CLE-ORDRE"), headers=ENTETES)
        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT statut, tentatives FROM envoi "
                        "WHERE cle_idempotence = 'CLE-ORDRE'")
            ligne = cur.fetchone()
        assert ligne["statut"] == "accepte"
        assert ligne["tentatives"] == 1


class TestVieprivee:
    def test_le_registre_ne_porte_aucune_adresse_en_clair(self, client_http, base):
        """Une base aspirée ailleurs ne doit pas devenir un fichier d'adresses."""
        adresse = "personne.privee@exemple.test"
        client_http.post("/api/v1/envois", json=demande("CLE-VP", adresse),
                         headers=ENTETES)
        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT adresse_empreinte FROM envoi "
                        "WHERE cle_idempotence = 'CLE-VP'")
            empreinte = cur.fetchone()["adresse_empreinte"]

        assert adresse not in empreinte
        # Poivrée, et pas seulement condensée. Un SHA-256 nu d'adresse se retrouve
        # par dictionnaire : l'espace des adresses plausibles est petit, et une
        # copie de la base suffirait à les reconstituer toutes.
        assert empreinte != hashlib.sha256(adresse.encode()).hexdigest()
        assert empreinte.startswith("v1:"), "La version prépare la rotation du poivre"

    def test_le_fil_d_exploitation_ne_rend_aucune_adresse(self, client_http):
        client_http.post("/api/v1/envois", json=demande("CLE-FIL"), headers=ENTETES)
        corps = client_http.get("/api/v1/envois", headers=ENTETES).text
        assert "tresorier@paroisse.test" not in corps


class TestBlocage:
    def test_une_adresse_morte_est_retenue_et_ne_recoit_plus_rien(
            self, client_http, base):
        """Continuer d'écrire à une adresse morte dégrade la remise de tout le reste."""
        mort = FournisseurObserve("mort", bloque=True)
        registre = Registre()
        registre.enregistrer(mort)
        from fastapi.testclient import TestClient
        client = TestClient(creer_application(
            Configuration(dsn="", secret_appel=SECRET_APPEL),
            ouvrir_connexion=base, registre=registre), raise_server_exceptions=False)

        premiere = client.post(
            "/api/v1/envois", json=demande("CLE-MORT", "morte@exemple.test"),
            headers=ENTETES)
        assert premiere.status_code == 409
        assert premiere.json()["reessayable"] is False

        # Le blocage survit à l'envoi : le suivant n'atteint même pas le fournisseur.
        avant = len(mort.envois)
        seconde = client.post(
            "/api/v1/envois", json=demande("CLE-MORT-2", "morte@exemple.test"),
            headers=ENTETES)
        assert seconde.status_code == 409
        assert len(mort.envois) == avant, "Rien ne doit repartir vers une adresse bloquée"

    def test_la_casse_de_l_adresse_ne_contourne_pas_le_blocage(self, client_http, base):
        from passerelle.journal import Journal

        with base() as conn:
            journal = Journal(conn)
            with journal.transaction() as cur:
                journal.bloquer(cur, Canal.COURRIEL, "Casse@Exemple.test", "désabonné")

        reponse = client_http.post(
            "/api/v1/envois", json=demande("CLE-CASSE", "casse@exemple.TEST"),
            headers=ENTETES)
        assert reponse.status_code == 409

    def test_un_exploitant_peut_lever_un_blocage(self, client_http, base):
        """Une adresse corrigée existe : un blocage sans levée condamne au silence."""
        from passerelle.journal import Journal

        with base() as conn:
            journal = Journal(conn)
            with journal.transaction() as cur:
                journal.bloquer(cur, Canal.COURRIEL, "levee@exemple.test", "erreur")

        leve = client_http.request(
            "DELETE", "/api/v1/blocages/courriel",
            json={"adresse": "levee@exemple.test"}, headers=ENTETES)
        assert leve.json()["debloquee"] is True

        passe = client_http.post(
            "/api/v1/envois", json=demande("CLE-LEVEE", "levee@exemple.test"),
            headers=ENTETES)
        assert passe.status_code == 200


class TestRepli:
    def test_un_fournisseur_en_panne_ne_fait_pas_taire_la_plateforme(self, base):
        casse = FournisseurObserve("casse", panne="Passerelle indisponible")
        secours = FournisseurObserve("secours")
        registre = Registre()
        registre.enregistrer(casse, rang=10)
        registre.enregistrer(secours, rang=50)
        from fastapi.testclient import TestClient
        client = TestClient(creer_application(
            Configuration(dsn="", secret_appel=SECRET_APPEL),
            ouvrir_connexion=base, registre=registre), raise_server_exceptions=False)

        reponse = client.post("/api/v1/envois", json=demande("CLE-REPLI"),
                              headers=ENTETES)
        assert reponse.status_code == 200
        assert reponse.json()["fournisseur"] == "secours"
        assert len(casse.envois) == 1, "Le premier doit avoir été essayé"

    def test_la_tentative_echouee_est_tracee(self, base):
        """Sans trace des échecs, on voit le résultat sans voir la dégradation."""
        casse = FournisseurObserve("casse2", panne="Refus 503")
        secours = FournisseurObserve("secours2")
        registre = Registre()
        registre.enregistrer(casse, rang=10)
        registre.enregistrer(secours, rang=50)
        from fastapi.testclient import TestClient
        client = TestClient(creer_application(
            Configuration(dsn="", secret_appel=SECRET_APPEL),
            ouvrir_connexion=base, registre=registre), raise_server_exceptions=False)
        client.post("/api/v1/envois", json=demande("CLE-TRACE"), headers=ENTETES)

        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT fournisseur, resultat FROM tentative "
                        "WHERE cle_idempotence = 'CLE-TRACE' ORDER BY faite_le")
            tentatives = [(t["fournisseur"], t["resultat"]) for t in cur.fetchall()]
        assert tentatives == [("casse2", "echec"), ("secours2", "accepte")]

    def test_tous_en_panne_rend_une_erreur_reessayable(self, base):
        casse = FournisseurObserve("seul", panne="Indisponible")
        registre = Registre()
        registre.enregistrer(casse)
        from fastapi.testclient import TestClient
        client = TestClient(creer_application(
            Configuration(dsn="", secret_appel=SECRET_APPEL),
            ouvrir_connexion=base, registre=registre), raise_server_exceptions=False)

        reponse = client.post("/api/v1/envois", json=demande("CLE-TOUS"),
                              headers=ENTETES)
        assert reponse.status_code == 502
        assert reponse.json()["reessayable"] is True

    def test_un_canal_sans_fournisseur_le_dit_clairement(self, client_http):
        """Le message n'est pas perdu : il n'a jamais été accepté."""
        reponse = client_http.post(
            "/api/v1/envois",
            json={**demande("CLE-SMS"), "canal": "sms"}, headers=ENTETES)
        assert reponse.status_code == 503
        assert reponse.json()["reessayable"] is False

    def test_un_canal_inconnu_est_refuse_avant_toute_ecriture(self, client_http, base):
        reponse = client_http.post(
            "/api/v1/envois",
            json={**demande("CLE-INCONNU"), "canal": "pigeon"}, headers=ENTETES)
        assert reponse.status_code == 422
        with base() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM envoi "
                        "WHERE cle_idempotence = 'CLE-INCONNU'")
            assert cur.fetchone()["n"] == 0


class TestExploitation:
    def test_les_indicateurs_montrent_l_etat_par_canal(self, client_http):
        client_http.post("/api/v1/envois", json=demande("CLE-IND"), headers=ENTETES)
        corps = client_http.get("/api/v1/indicateurs", headers=ENTETES).json()
        assert corps["par_canal"]["courriel"]["accepte"] >= 1
        assert "courriel" in corps["canaux_servis"]

    def test_un_canal_a_fournisseur_unique_est_signale(self, client_http):
        """Il se tait entièrement le jour où son seul fournisseur tombe."""
        anomalies = client_http.get(
            "/api/v1/indicateurs", headers=ENTETES).json()["anomalies"]
        assert any("qu'un fournisseur" in a for a in anomalies)

    def test_la_sonde_ne_dit_pas_par_ou_passent_nos_messages(self, client_http):
        """Une route ouverte qui nomme les fournisseurs et ce qui manque renseigne
        qui la lit sur la surface à viser. Le détail est derrière le secret."""
        corps = client_http.get("/health").json()
        assert set(corps) == {"service", "canaux"}

    def test_l_etat_d_un_envoi_se_relit_par_sa_cle(self, client_http):
        client_http.post("/api/v1/envois", json=demande("CLE-RELUE"), headers=ENTETES)
        corps = client_http.get("/api/v1/envois/CLE-RELUE", headers=ENTETES).json()
        assert corps["statut"] == "accepte"
        assert corps["organisation"] == "paroisse-saint-jean"

    def test_une_cle_inconnue_rend_404(self, client_http):
        assert client_http.get(
            "/api/v1/envois/JAMAIS-VUE", headers=ENTETES).status_code == 404
