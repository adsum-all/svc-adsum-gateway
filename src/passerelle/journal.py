"""Le registre des envois : la mémoire de la passerelle.

Sans lui, la passerelle n'est qu'un relais, et un relais ne sait pas répondre aux
deux seules questions qu'on lui posera jamais : « avez-vous envoyé ce message ? » et
« l'avez-vous envoyé deux fois ? ».

Deux mécanismes, et le second dépend du premier.

**La réservation avant l'envoi.** La clé d'idempotence est écrite en base *avant*
d'appeler le fournisseur, avec un index unique. Deux appels simultanés portant la
même clé se disputent l'insertion ; l'un gagne et envoie, l'autre voit le conflit et
rend le résultat du premier. L'ordre inverse, envoyer puis écrire, laisse la fenêtre
pendant laquelle deux mises en demeure partent.

**La reprise d'une réservation abandonnée.** Un service tué entre la réservation et
l'envoi laisse une ligne « en attente » qui bloquerait la clé pour toujours. Une
réservation plus vieille que son délai est donc reprenable : la deuxième tentative
la reprend et envoie. Le risque assumé est un doublon quand le premier envoi était
en fait parti, ce qui vaut mieux qu'une relance jamais partie.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .port import AccuseReception, Canal, Envoi, Statut


class DejaEnvoye(Exception):
    """Cette clé a déjà produit un envoi. Porte le résultat du premier."""

    def __init__(self, envoi: Envoi) -> None:
        super().__init__(f"Envoi déjà effectué pour « {envoi.cle_idempotence} »")
        self.envoi = envoi


class DejaTraite(Exception):
    """Cet accusé a déjà été appliqué. Ce n'est pas une erreur."""


#: Durées de conservation, en jours. Écrites ici et appliquées par une tâche : une
#: durée qui n'existe que dans une politique n'est pas une durée.
#:
#: Treize mois pour les envois : de quoi couvrir un exercice comptable complet et la
#: réclamation qui arrive après. Quatre-vingt-dix jours pour les tentatives, qui ne
#: servent qu'à comprendre une panne en cours.
CONSERVATION_ENVOIS_J = 395
CONSERVATION_TENTATIVES_J = 90

#: Au-delà, une réservation sans envoi est considérée comme abandonnée par un
#: processus mort. Cinq minutes : plus long que tout appel de fournisseur, assez
#: court pour qu'une relance ne dorme pas une journée.
DELAI_RESERVATION_S = 300


class Journal:
    """Accès à la base de la passerelle. La connexion est fournie, jamais créée ici."""

    def __init__(self, connexion: Any) -> None:
        self._c = connexion

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        curseur = self._c.cursor()
        try:
            yield curseur
            self._c.commit()
        except Exception:
            self._c.rollback()
            raise
        finally:
            curseur.close()

    # -- Réservation ---------------------------------------------------------

    def reserver(
        self, cur: Any, cle: str, canal: Canal, adresse: str,
        categorie: str, organisation: str, appelant: str = "inconnu",
    ) -> Envoi | None:
        """Prendre la clé. Rend l'envoi déjà fait s'il y en a un, sinon None.

        L'adresse est écrite condensée et non en clair : le registre est consulté par
        des exploitants qui n'ont aucune raison de lire l'adresse personnelle d'un
        administrateur client, et un journal aspiré ailleurs ne doit pas devenir un
        fichier d'adresses. Le condensé suffit à répondre à « avons-nous écrit à
        cette adresse », qui est la seule question utile.
        """
        cur.execute(
            "INSERT INTO envoi (cle_idempotence, canal, adresse_empreinte, categorie, "
            "  organisation, appelant, statut) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'en_attente') "
            "ON CONFLICT (cle_idempotence) DO NOTHING RETURNING id",
            (cle, canal.value, _empreinte(adresse), categorie, organisation, appelant),
        )
        if cur.fetchone() is not None:
            return None

        # La clé existe déjà. Soit l'envoi est fait, soit une réservation traîne.
        cur.execute(
            "SELECT statut, fournisseur, canal, reference, envoye_le, motif, "
            "       (now() - cree_le) > make_interval(secs => %s) AS perimee "
            "  FROM envoi WHERE cle_idempotence = %s FOR UPDATE",
            (DELAI_RESERVATION_S, cle),
        )
        ligne = cur.fetchone()
        if ligne is None:
            # Effacée entre l'insertion et la lecture. Rarissime, et la conduite
            # sûre est de laisser l'appelant réessayer plutôt que d'envoyer à
            # l'aveugle.
            return None

        # Deux cas se reprennent, et il a fallu une revue pour voir que le second
        # manquait.
        #
        # Une réservation « en attente » périmée : un processus mort entre la
        # réservation et l'envoi. Le risque assumé est un doublon si le premier envoi
        # était en fait parti, ce qui vaut mieux qu'une relance jamais partie.
        #
        # Un envoi « refusé » : tous les fournisseurs ont échoué. La route répond
        # alors à l'appelant que l'erreur est réessayable, et la clé restait pourtant
        # morte pour toujours. Le service promettait une reprise qu'il refusait
        # ensuite en silence, et la relance ne repartait jamais.
        if ligne["statut"] in ("en_attente", "refuse") and ligne["perimee"]:
            cur.execute(
                "UPDATE envoi SET cree_le = now() WHERE cle_idempotence = %s", (cle,))
            return None

        return Envoi(
            cle_idempotence=cle,
            statut=Statut(ligne["statut"]),
            fournisseur=ligne["fournisseur"] or "",
            canal=Canal(ligne["canal"]),
            reference=ligne["reference"] or "",
            envoye_le=ligne["envoye_le"],
            motif=ligne["motif"],
            deja_fait=True,
        )

    def conclure(self, cur: Any, envoi: Envoi, tentatives: int = 1) -> None:
        """Écrire le résultat de l'envoi sur la réservation."""
        cur.execute(
            "UPDATE envoi SET statut = %s, fournisseur = %s, reference = %s, "
            "  envoye_le = %s, motif = %s, tentatives = %s, maj_le = now() "
            "WHERE cle_idempotence = %s",
            (envoi.statut.value, envoi.fournisseur, envoi.reference,
             envoi.envoye_le, _tronquer(envoi.motif), tentatives, envoi.cle_idempotence),
        )

    def abandonner(self, cur: Any, cle: str, motif: str, tentatives: int) -> None:
        """Marquer l'échec définitif après épuisement des fournisseurs.

        La réservation n'est pas effacée : une clé qui redevient libre laisse
        repartir le même message à la tentative suivante, ce qui est exactement le
        doublon que tout ceci existe pour empêcher.
        """
        cur.execute(
            "UPDATE envoi SET statut = 'refuse', motif = %s, tentatives = %s, "
            "  maj_le = now() WHERE cle_idempotence = %s",
            (_tronquer(motif), tentatives, cle),
        )

    # -- Adresses bloquées ---------------------------------------------------

    def bloquer(self, cur: Any, canal: Canal, adresse: str, motif: str) -> None:
        """Retenir qu'une adresse ne doit plus rien recevoir.

        Un désabonnement ou une adresse morte. Continuer d'écrire abîme la réputation
        d'envoi du domaine, ce qui finit par envoyer en indésirables les messages
        destinés à tous les autres.
        """
        cur.execute(
            "INSERT INTO adresse_bloquee (canal, adresse_empreinte, motif) "
            "VALUES (%s, %s, %s) ON CONFLICT (canal, adresse_empreinte) "
            "DO UPDATE SET motif = EXCLUDED.motif, bloquee_le = now()",
            (canal.value, _empreinte(adresse), _tronquer(motif)),
        )

    def est_bloquee(self, cur: Any, canal: Canal, adresse: str) -> str | None:
        cur.execute(
            "SELECT motif FROM adresse_bloquee "
            "WHERE canal = %s AND adresse_empreinte = %s",
            (canal.value, _empreinte(adresse)),
        )
        ligne = cur.fetchone()
        return (ligne["motif"] or "adresse bloquée") if ligne else None

    def debloquer(self, cur: Any, canal: Canal, adresse: str) -> bool:
        """Lever un blocage. Réservé à un exploitant : une adresse corrigée existe."""
        cur.execute(
            "DELETE FROM adresse_bloquee WHERE canal = %s AND adresse_empreinte = %s",
            (canal.value, _empreinte(adresse)),
        )
        return cur.rowcount > 0

    # -- Accusés -------------------------------------------------------------

    def appliquer_accuse(self, cur: Any, accuse: AccuseReception) -> bool:
        """Poser le statut rendu par un fournisseur. Rend faux si rien n'a changé.

        Trois protections, et deux d'entre elles ont été ajoutées après revue.

        **Déduplication par l'identifiant d'événement**, avec un index unique et non
        une lecture préalable : entre un SELECT et un INSERT, une seconde livraison
        passe. Quand le fournisseur ne donne aucun identifiant, il est dérivé du
        contenu : sans cela, un accusé sans identifiant se rejoue indéfiniment,
        c'est-à-dire que l'émetteur choisit lui-même s'il veut être dédupliqué.

        **Un blocage exige un envoi réel vers cette adresse.** Brevo n'a pas de
        signature : son accusé n'est authentifié que par un secret statique qui ne
        couvre pas le corps. Un corps forgé pouvait donc bloquer définitivement
        n'importe quelle adresse, c'est-à-dire faire taire la plateforme pour un
        destinataire choisi. On ne bloque plus que ce à quoi l'on a réellement écrit.

        **Un accusé qui n'a rien changé n'est pas retenu comme traité.** Le marqueur
        de déduplication était écrit avant l'application : quand la mise à jour ne
        touchait aucune ligne, l'accusé était perdu et aucune relivraison ne pouvait
        le rattraper.
        """
        identifiant = accuse.identifiant_evenement or _identifiant_derive(accuse)
        cur.execute(
            "INSERT INTO accuse (fournisseur, identifiant_evenement, statut) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
            (accuse.fournisseur, identifiant, accuse.statut.value),
        )
        if cur.fetchone() is None:
            raise DejaTraite(f"Accusé {identifiant} déjà appliqué")

        bloque = False
        if accuse.statut is Statut.BLOQUE and accuse.adresse:
            envoi = _envoi_de(cur, accuse.fournisseur, accuse.reference)
            if envoi is not None and envoi["adresse_empreinte"] == _empreinte(accuse.adresse):
                self.bloquer(
                    cur, Canal(envoi["canal"]), accuse.adresse,
                    sans_adresse(accuse.motif, accuse.adresse)
                    or "signalé par le fournisseur")
                bloque = True

        if not accuse.reference:
            if not bloque:
                # Rien n'a changé. Retirer le marqueur pour qu'une relivraison
                # correcte puisse encore être appliquée.
                cur.execute(
                    "DELETE FROM accuse WHERE fournisseur = %s "
                    "  AND identifiant_evenement = %s",
                    (accuse.fournisseur, identifiant))
            return bloque

        cur.execute(
            # Un statut terminal ne redescend pas : un accusé tardif « accepté »
            # arrivant après une remise confirmée effacerait la preuve de remise.
            "UPDATE envoi SET statut = %s, motif = COALESCE(%s, motif), maj_le = now() "
            "WHERE reference = %s AND fournisseur = %s "
            "  AND statut NOT IN ('remis', 'bloque') RETURNING id",
            (accuse.statut.value,
             _tronquer(sans_adresse(accuse.motif, accuse.adresse)),
             accuse.reference, accuse.fournisseur),
        )
        applique = cur.fetchone() is not None
        if not applique and not bloque:
            cur.execute(
                "DELETE FROM accuse WHERE fournisseur = %s "
                "  AND identifiant_evenement = %s",
                (accuse.fournisseur, identifiant))
        return applique or bloque

    # -- Lecture -------------------------------------------------------------

    def etat(self, cur: Any, cle: str) -> dict[str, Any] | None:
        cur.execute(
            "SELECT cle_idempotence, canal, statut, fournisseur, reference, "
            "       categorie, organisation, appelant, tentatives, envoye_le, "
            "       motif, cree_le "
            "  FROM envoi WHERE cle_idempotence = %s",
            (cle,),
        )
        ligne = cur.fetchone()
        return _envoi_json(ligne) if ligne else None

    def derniers(
        self, cur: Any, organisation: str = "", canal: str = "", limite: int = 50,
    ) -> list[dict[str, Any]]:
        """Les envois récents, pour l'exploitation. Jamais d'adresse en clair."""
        limite = max(1, min(limite, 200))
        cur.execute(
            "SELECT cle_idempotence, canal, statut, fournisseur, reference, "
            "       categorie, organisation, appelant, tentatives, envoye_le, "
            "       motif, cree_le "
            "  FROM envoi "
            " WHERE (%s = '' OR organisation = %s) AND (%s = '' OR canal = %s) "
            " ORDER BY cree_le DESC LIMIT %s",
            (organisation, organisation, canal, canal, limite),
        )
        return [_envoi_json(ligne) for ligne in cur.fetchall()]

    def indicateurs(self, cur: Any, heures: int = 24) -> dict[str, Any]:
        """De quoi voir qu'un canal se dégrade avant que le client ne le signale."""
        heures = max(1, min(heures, 720))
        cur.execute(
            "SELECT canal, statut, count(*) AS n FROM envoi "
            " WHERE cree_le > now() - make_interval(hours => %s) "
            " GROUP BY canal, statut",
            (heures,),
        )
        par_canal: dict[str, dict[str, int]] = {}
        for ligne in cur.fetchall():
            par_canal.setdefault(ligne["canal"], {})[ligne["statut"]] = ligne["n"]
        cur.execute("SELECT count(*) AS n FROM adresse_bloquee")
        return {
            "fenetre_heures": heures,
            "par_canal": par_canal,
            "adresses_bloquees": cur.fetchone()["n"],
        }

    def purger(self, cur: Any, jours_envois: int, jours_tentatives: int) -> dict[str, int]:
        """Effacer ce qui a dépassé sa durée de conservation.

        Le registre accumulait indéfiniment. Conserver sans limite une donnée
        personnelle, fût-elle condensée, contrevient au principe de limitation de la
        conservation : une durée doit être définie, et surtout appliquée.

        Deux durées, parce que les deux tables ne servent pas à la même chose. Les
        envois répondent à « avez-vous écrit à ce client », question qui se pose
        jusqu'à un exercice comptable plus tard. Les tentatives servent à comprendre
        une panne en cours ; au-delà de quelques semaines, personne ne les relit.

        Les adresses bloquées ne sont jamais purgées : un désabonnement est
        définitif, et l'oublier ferait repartir des messages vers quelqu'un qui a
        demandé à ne plus en recevoir.
        """
        cur.execute(
            "DELETE FROM tentative WHERE faite_le < now() - make_interval(days => %s)",
            (max(1, jours_tentatives),))
        tentatives = cur.rowcount

        # Les tentatives d'un envoi partent avec lui, sinon elles resteraient
        # rattachées à une clé qui n'existe plus.
        cur.execute(
            "DELETE FROM tentative WHERE cle_idempotence IN ("
            "  SELECT cle_idempotence FROM envoi "
            "   WHERE cree_le < now() - make_interval(days => %s))",
            (max(1, jours_envois),))
        tentatives += cur.rowcount

        cur.execute(
            "DELETE FROM envoi WHERE cree_le < now() - make_interval(days => %s)",
            (max(1, jours_envois),))
        envois = cur.rowcount

        cur.execute(
            "DELETE FROM accuse WHERE recu_le < now() - make_interval(days => %s)",
            (max(1, jours_envois),))
        accuses = cur.rowcount

        return {"envois": envois, "tentatives": tentatives, "accuses": accuses}

    def tracer(
        self, cur: Any, cle: str, fournisseur: str, resultat: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Une ligne par tentative, y compris celles qui ont échoué.

        C'est ce qui permet de dire « le premier fournisseur a refusé, le second a
        pris » plutôt que de constater seulement le résultat final.
        """
        cur.execute(
            "INSERT INTO tentative (cle_idempotence, fournisseur, resultat, detail) "
            "VALUES (%s, %s, %s, %s)",
            (cle, fournisseur, resultat[:200],
             json.dumps(detail or {}, ensure_ascii=False)),
        )


def _envoi_de(cur: Any, fournisseur: str, reference: str) -> dict[str, Any] | None:
    """L'envoi auquel un accusé se rattache, s'il en existe un.

    Le fournisseur est comparé lui aussi : deux fournisseurs peuvent rendre la même
    référence, et sans cette condition l'accusé de l'un s'appliquerait à l'envoi de
    l'autre.
    """
    if not reference:
        return None
    cur.execute(
        "SELECT canal, adresse_empreinte FROM envoi "
        "WHERE reference = %s AND fournisseur = %s LIMIT 1",
        (reference, fournisseur))
    ligne = cur.fetchone()
    return dict(ligne) if ligne else None


def _identifiant_derive(accuse: AccuseReception) -> str:
    """Un identifiant de déduplication quand le fournisseur n'en donne aucun.

    Dérivé du contenu qui compte : sans lui, un accusé sans identifiant se rejoue
    indéfiniment, ce qui revient à laisser l'émetteur décider s'il veut être
    dédupliqué. Deux accusés réellement distincts sur le même envoi diffèrent par
    leur statut ou leur motif, donc par leur empreinte.
    """
    import hashlib

    graine = "|".join((
        accuse.fournisseur, accuse.reference, accuse.statut.value,
        accuse.adresse, accuse.motif or ""))
    return "derive-" + hashlib.sha256(graine.encode("utf-8")).hexdigest()[:40]


def sans_adresse(motif: str | None, adresse: str = "") -> str | None:
    """Le motif du fournisseur, débarrassé de l'adresse qu'il recopie.

    Un motif de rebond porte presque toujours l'adresse en clair, et il était stocké
    tel quel dans la colonne voisine du condensé : le registre reconstituait donc de
    lui-même ce que le condensé existait pour cacher.
    """
    if not motif:
        return motif
    # Tronqué d'abord. L'expression régulière plus bas est quadratique, et le motif
    # vient d'un corps que l'appelant contrôle : sur quelques mégaoctets, elle
    # occupe le processeur de l'instance. Cinq cents caractères sont de toute façon
    # tout ce qui sera conservé.
    propre = motif[:500]
    for forme in {adresse, adresse.strip().lower()} - {""}:
        propre = propre.replace(forme, "[adresse]")
    # Un motif peut aussi nommer une adresse voisine, celle d'un renvoi par exemple.
    # Toute forme reconnaissable est retirée, pas seulement celle que l'on attendait.
    import re

    return re.sub(r"[\w.+-]+@[\w.-]+\.\w+", "[adresse]", propre)


#: Le poivre, lu une fois. Il vit hors de la base, dans l'environnement du service.
#: Sans lui, le condensé ne protège pas contre le scénario pour lequel il existe.
_POIVRE: bytes | None = None


def poivre() -> bytes:
    """Le secret qui rend le condensé d'adresse inexploitable hors du service.

    Un SHA-256 nu d'adresse de courriel ne protège rien. L'espace des adresses
    plausibles est petit : qui détient une copie de la table les retrouve par
    dictionnaire, à plusieurs centaines de milliers d'essais par seconde sur un
    poste ordinaire, et peut de toute façon tester l'appartenance de n'importe
    quelle adresse devinée. La colonne restait donc une donnée personnelle
    pseudonymisée, présentée comme une garantie de non-conservation.

    Avec un poivre gardé hors de la base, une copie de la base seule ne rend plus
    rien. Le service, lui, continue de reconnaître une adresse qu'on lui soumet,
    ce qui est le seul usage dont il a besoin.

    Le défaut est refusé plutôt que remplacé par une valeur vide : un poivre absent
    ramènerait silencieusement au condensé nu, et personne ne le verrait.
    """
    global _POIVRE
    if _POIVRE is None:
        import os

        valeur = os.environ.get("ADSUM_PASSERELLE_POIVRE", "")
        if len(valeur) < 32:
            raise RuntimeError(
                "ADSUM_PASSERELLE_POIVRE absent ou trop court (32 caractères au "
                "moins). Sans lui, les adresses du registre se retrouvent par "
                "dictionnaire à partir d'une simple copie de la base.")
        _POIVRE = valeur.encode("utf-8")
    return _POIVRE


def _empreinte(adresse: str) -> str:
    """Le condensé d'une adresse : poivré, et normalisé d'abord.

    Normalisé, sinon « Tresorier@Exemple.test » et « tresorier@exemple.test »
    donneraient deux empreintes, et une adresse bloquée continuerait de recevoir
    sous une autre casse.

    Le préfixe de version prépare la rotation : le jour où le poivre change, les
    anciennes empreintes restent reconnaissables comme telles au lieu de devenir
    des valeurs qui ne correspondent plus à rien sans qu'on sache pourquoi.
    """
    import hashlib
    import hmac

    condense = hmac.new(
        poivre(), adresse.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1:{condense}"


def _tronquer(valeur: str | None) -> str | None:
    return valeur[:500] if valeur else None


def _envoi_json(ligne: Any) -> dict[str, Any]:
    def date(valeur: datetime | None) -> str | None:
        return valeur.isoformat() if valeur else None

    return {
        "cle": ligne["cle_idempotence"],
        "canal": ligne["canal"],
        "statut": ligne["statut"],
        "fournisseur": ligne["fournisseur"],
        "reference": ligne["reference"],
        "categorie": ligne["categorie"],
        "organisation": ligne["organisation"],
        "appelant": ligne["appelant"],
        "tentatives": ligne["tentatives"],
        "envoye_le": date(ligne["envoye_le"]),
        "cree_le": date(ligne["cree_le"]),
        "motif": ligne["motif"],
    }
