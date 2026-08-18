"""L'acheminement : ce que la passerelle fait vraiment d'un message.

Cinq décisions, dans cet ordre, et l'ordre compte autant que les décisions.

**Le destinataire est-il bloqué ?** Avant tout le reste. Écrire à une adresse morte
ou désabonnée abîme la réputation d'envoi du domaine, ce qui finit par envoyer en
indésirables les messages destinés à tous les autres. Un blocage n'est pas une
erreur de la passerelle : c'est une réponse, et elle se lit dans le registre.

**La clé a-t-elle déjà servi ?** Réservée en base avant le moindre appel réseau.
Deux appels simultanés se disputent l'insertion : l'un envoie, l'autre rend le
résultat du premier. L'ordre inverse laisse partir deux mises en demeure.

**Quel fournisseur ?** Le registre classe ceux qui servent le canal. Le premier est
essayé, et son échec fait passer au suivant : un service qui n'a qu'une route se tait
entièrement le jour où elle tombe.

**Un refus définitif arrête tout.** Un destinataire bloqué chez le premier
fournisseur le sera chez le second. Continuer la liste ne fait que multiplier les
signalements contre notre domaine.

**Tout est tracé, y compris les échecs.** Une tentative par ligne. Sans elles, on
constate le résultat final sans jamais voir qu'un canal se dégrade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .journal import (
    CONSERVATION_ENVOIS_J,
    CONSERVATION_TENTATIVES_J,
    DejaTraite,
    Journal,
    sans_adresse,
)
from .port import (
    AccuseReception,
    Canal,
    CanalNonServi,
    DestinataireBloque,
    Envoi,
    ErreurEnvoi,
    Fournisseur,
    Message,
    Statut,
)


@dataclass
class Registre:
    """Les fournisseurs configurés et l'ordre dans lequel les essayer."""

    fournisseurs: dict[str, Fournisseur] = field(default_factory=dict)
    #: Le rang de chacun. Plus bas passe en premier. Le classement est recalculé à
    #: la lecture plutôt que maintenu à l'écriture : une liste ordonnée entretenue
    #: à la main se désordonne au premier ajout fait dans le mauvais sens.
    rangs: dict[str, int] = field(default_factory=dict)

    def enregistrer(self, fournisseur: Fournisseur, rang: int = 100) -> None:
        """Ajouter un fournisseur. Ses canaux déclarés deviennent joignables."""
        self.fournisseurs[fournisseur.code] = fournisseur
        self.rangs[fournisseur.code] = rang

    def pour(self, canal: Canal) -> list[Fournisseur]:
        """Ceux qui servent ce canal, le meilleur d'abord."""
        candidats = [f for f in self.fournisseurs.values() if canal in f.canaux]
        candidats.sort(key=lambda f: (self.rangs.get(f.code, 100), f.code))
        return candidats

    def canaux_servis(self) -> list[str]:
        return sorted(canal.value for canal in Canal if self.pour(canal))

    def controle_de_coherence(self) -> list[str]:
        """Les défauts de configuration, listés plutôt que levés.

        Exécuté au démarrage et journalisé. Une passerelle qui refuse de démarrer
        parce qu'un canal n'a pas de fournisseur est pire que celle qui le dit et
        sert les autres.
        """
        anomalies = []
        for canal in Canal:
            candidats = self.pour(canal)
            if not candidats:
                anomalies.append(f"Le canal « {canal.value} » n'a aucun fournisseur")
            elif len(candidats) == 1:
                anomalies.append(
                    f"Le canal « {canal.value} » n'a qu'un fournisseur : "
                    f"il se tait entièrement si « {candidats[0].code} » tombe")
        return anomalies


class ServicePasserelle:
    """L'acheminement, sans dépendance à un cadre web."""

    def __init__(self, journal: Journal, registre: Registre) -> None:
        self._journal = journal
        self._registre = registre

    def envoyer(self, message: Message, appelant: str = "inconnu") -> Envoi:
        candidats = self._registre.pour(message.canal)
        if not candidats:
            raise CanalNonServi(
                f"Aucun fournisseur ne sert le canal « {message.canal.value} ». "
                "Le message n'est pas perdu : il n'a jamais été accepté.")

        with self._journal.transaction() as cur:
            blocage = self._journal.est_bloquee(
                cur, message.canal, message.destinataire.adresse)
            if blocage is not None:
                raise DestinataireBloque(
                    f"Ce destinataire ne reçoit plus rien sur ce canal : {blocage}")

            deja = self._journal.reserver(
                cur, message.cle_idempotence, message.canal,
                message.destinataire.adresse, message.categorie, message.organisation,
                appelant)
            if deja is not None:
                return deja

        # Hors transaction : un appel réseau tenu à l'intérieur garderait la ligne
        # verrouillée pendant tout le temps du fournisseur, et une passerelle lente
        # bloquerait alors tout le reste de la file.
        echecs: list[str] = []
        for tentative, fournisseur in enumerate(candidats, start=1):
            try:
                envoi = fournisseur.envoyer(message)
            except DestinataireBloque as e:
                # Définitif. Essayer le suivant ne ferait que multiplier les
                # signalements contre notre domaine.
                #
                # Le motif du fournisseur nomme presque toujours l'adresse rejetée.
                # Il est écrit dans trois tables et rendu dans une réponse HTTP :
                # sans expurgation, le registre reconstituait de lui-même ce que le
                # condensé existe pour cacher, et l'adresse traversait jusque dans
                # la base du service commerce.
                propre = sans_adresse(str(e), message.destinataire.adresse) or ""
                with self._journal.transaction() as cur:
                    self._journal.tracer(
                        cur, message.cle_idempotence, fournisseur.code, "bloque")
                    self._journal.bloquer(
                        cur, message.canal, message.destinataire.adresse, propre)
                    self._journal.conclure(cur, Envoi(
                        cle_idempotence=message.cle_idempotence,
                        statut=Statut.BLOQUE, fournisseur=fournisseur.code,
                        canal=message.canal, motif=propre), tentative)
                raise DestinataireBloque(propre) from None
            except ErreurEnvoi as e:
                propre = sans_adresse(str(e), message.destinataire.adresse) or ""
                echecs.append(f"{fournisseur.code} : {propre}")
                with self._journal.transaction() as cur:
                    self._journal.tracer(
                        cur, message.cle_idempotence, fournisseur.code,
                        "echec", {"motif": propre[:300]})
                continue
            except Exception as e:  # noqa: BLE001
                # Large volontairement. Les adaptateurs traduisent ce qu'ils
                # attendent, mais le transport laisse remonter ses propres
                # exceptions : un délai dépassé chez un fournisseur sortait d'ici
                # sans être rattrapé, ce qui annulait le repli sur le suivant,
                # n'écrivait aucune trace, et laissait la réservation en attente. Le
                # message ne partait jamais, ou partait deux fois à la reprise.
                #
                # Le type est conservé, le détail non : une exception de
                # bibliothèque HTTP porte volontiers l'URL complète et les en-têtes,
                # secret compris.
                echecs.append(f"{fournisseur.code} : {type(e).__name__}")
                with self._journal.transaction() as cur:
                    self._journal.tracer(
                        cur, message.cle_idempotence, fournisseur.code,
                        "panne", {"type": type(e).__name__})
                continue

            with self._journal.transaction() as cur:
                self._journal.tracer(
                    cur, message.cle_idempotence, fournisseur.code, envoi.statut.value)
                self._journal.conclure(cur, envoi, tentative)
            return envoi

        motif = " | ".join(echecs)[:500]
        with self._journal.transaction() as cur:
            self._journal.abandonner(
                cur, message.cle_idempotence, motif, len(candidats))
        raise ErreurEnvoi(
            f"Aucun fournisseur n'a pu acheminer le message. {motif}")

    # -- Accusés -------------------------------------------------------------

    def recevoir_accuse(
        self, code_fournisseur: str, entetes: dict[str, str], corps: bytes,
    ) -> dict[str, Any]:
        """Authentifier puis appliquer. La base n'est ouverte qu'après.

        Découpé en deux à dessein. La connexion s'ouvrait auparavant avant même de
        savoir si le fournisseur existait et si la signature tenait : une requête
        forgée depuis Internet, sans aucun secret, provoquait donc un établissement
        complet de connexion PostgreSQL. Sur une base derrière un répartiteur, c'est
        une amplification directe vers la base, offerte à n'importe qui.
        """
        return self.appliquer_accuse(
            code_fournisseur, self.authentifier_accuse(code_fournisseur, entetes, corps))

    def authentifier_accuse(
        self, code_fournisseur: str, entetes: dict[str, str], corps: bytes,
    ) -> AccuseReception:
        """La partie qui ne touche pas la base. Lève si l'accusé n'est pas authentique."""
        fournisseur = self._registre.fournisseurs.get(code_fournisseur)
        if fournisseur is None:
            raise ErreurEnvoi("Fournisseur inconnu.")
        return fournisseur.verifier_accuse(entetes, corps)

    def appliquer_accuse(
        self, code_fournisseur: str, accuse: AccuseReception,
    ) -> dict[str, Any]:
        """La partie qui écrit. N'est atteinte qu'après authentification."""
        with self._journal.transaction() as cur:
            try:
                applique = self._journal.appliquer_accuse(cur, accuse)
            except DejaTraite:
                return {"traite": False, "raison": "accusé déjà appliqué"}
        return {"traite": applique, "statut": accuse.statut.value}

    # -- Lecture -------------------------------------------------------------

    def etat(self, cle: str) -> dict[str, Any] | None:
        with self._journal.transaction() as cur:
            return self._journal.etat(cur, cle)

    def derniers(self, organisation: str, canal: str, limite: int) -> list[dict[str, Any]]:
        with self._journal.transaction() as cur:
            return self._journal.derniers(cur, organisation, canal, limite)

    def indicateurs(self, heures: int) -> dict[str, Any]:
        with self._journal.transaction() as cur:
            indicateurs = self._journal.indicateurs(cur, heures)
        indicateurs["canaux_servis"] = self._registre.canaux_servis()
        indicateurs["anomalies"] = self._registre.controle_de_coherence()
        return indicateurs

    def purger(self, jours_envois: int = CONSERVATION_ENVOIS_J,
               jours_tentatives: int = CONSERVATION_TENTATIVES_J) -> dict[str, int]:
        """Appliquer les durées de conservation. Appelé par l'ordonnanceur."""
        with self._journal.transaction() as cur:
            return self._journal.purger(cur, jours_envois, jours_tentatives)

    def debloquer(self, canal: str, adresse: str) -> bool:
        with self._journal.transaction() as cur:
            return self._journal.debloquer(cur, Canal(canal), adresse)
