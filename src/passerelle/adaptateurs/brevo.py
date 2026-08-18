"""Brevo : le courriel transactionnel de l'éditeur.

Le fournisseur déjà en service pour la plateforme, repris ici avec **l'expéditeur de
l'éditeur** et non celui d'un client. C'est le défaut que cette passerelle corrige :
les relances d'impayé d'ADSUM partaient jusqu'ici par la chaîne d'envoi d'une
organisation cliente. Une paroisse voyait donc ses propres quotas consommés par la
facturation de son prestataire, et une mise en demeure arrivait signée du nom de
celui qui la reçoit.

Trois points techniques qui ne sont pas des détails.

**L'expéditeur doit être validé chez Brevo.** Une adresse non validée est acceptée
par l'API puis silencieusement rejetée à la remise. L'adaptateur exige donc que
l'adresse soit configurée explicitement, et ne devine jamais.

**Une adresse en liste noire n'est pas un échec ordinaire.** Brevo répond 400 avec
un code précis quand le destinataire s'est désabonné ou que l'adresse est morte.
Réessayer chez un autre fournisseur ne servirait à rien et abîmerait la réputation
d'envoi. Ce cas remonte comme « bloqué », et l'appelant ne réessaie pas.

**La clé d'idempotence ne voyage pas.** Brevo n'en accepte pas : la déduplication est
donc faite par la passerelle avant l'appel, dans son journal, et non déléguée.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..port import (
    AccuseReception,
    Canal,
    DestinataireBloque,
    Envoi,
    ErreurEnvoi,
    Fournisseur,
    Message,
    Statut,
)
from ..transport import Transport, TransportHttpx

API = "https://api.brevo.com/v3"


def _une_ligne(valeur: str) -> str:
    """Une valeur destinée à un en-tête, débarrassée de ce qui en fabriquerait un.

    Retour chariot, saut de ligne et caractère nul. Le fournisseur recopie ces
    champs dans les en-têtes du message qu'il émet, et un saut de ligne y ouvre
    une ligne d'en-tête supplémentaire.
    """
    interdits = str.maketrans({
        chr(13): ' ',    # retour chariot
        chr(10): ' ',    # saut de ligne
        chr(0): '',      # caractère nul
    })
    return valeur.translate(interdits)

#: Les codes que Brevo rend quand réessayer est inutile. Les traiter comme des
#: échecs ordinaires ferait boucler la file sur une adresse morte.
DEFINITIFS = frozenset({
    "invalid_parameter", "blocked_contact", "unsubscribed_recipient",
    "hard_bounce", "invalid_email",
})

#: Les événements d'accusé que Brevo pousse, traduits. Ceux qui manquent, ouverture
#: et clic notamment, ne sont pas suivis : mesurer qui ouvre un courriel de relance
#: n'a aucun usage légitime ici et alourdirait le registre de données inutiles.
EVENEMENTS = {
    "delivered": Statut.REMIS,
    "soft_bounce": Statut.REFUSE,
    "hard_bounce": Statut.BLOQUE,
    "blocked": Statut.BLOQUE,
    "unsubscribed": Statut.BLOQUE,
    "spam": Statut.BLOQUE,
    "invalid_email": Statut.BLOQUE,
    "deferred": Statut.ACCEPTE,
    "request": Statut.ACCEPTE,
}

DELAI_S = 20.0


class Brevo(Fournisseur):
    """Courriel transactionnel par Brevo, sous l'identité de l'éditeur."""

    code = "brevo"
    libelle = "Brevo"
    canaux = (Canal.COURRIEL,)

    def __init__(
        self, cle_api: str, expediteur: str, nom_expediteur: str,
        secret_accuse: str = "", transport: Transport | None = None,
        racine: str = API,
    ) -> None:
        if not cle_api or not expediteur:
            raise ValueError(
                "Brevo exige une clé d'API et une adresse d'expédition validée chez "
                "lui. Une adresse non validée est acceptée par l'API puis rejetée à "
                "la remise, et l'échec ne se voit nulle part."
            )
        self._cle = cle_api
        self._expediteur = expediteur
        self._nom = nom_expediteur or expediteur
        self._secret_accuse = secret_accuse
        self._transport = transport or TransportHttpx()
        self._racine = racine.rstrip("/")

    # -- Envoi ---------------------------------------------------------------

    def envoyer(self, message: Message) -> Envoi:
        if message.canal is not Canal.COURRIEL:
            raise ErreurEnvoi(f"Brevo ne sert pas le canal « {message.canal.value} ».")
        adresse = message.destinataire.adresse.strip()
        if "@" not in adresse or adresse.startswith("@") or adresse.endswith("@"):
            # L'adresse ne figure pas dans le message. Cette exception est écrite
            # dans le registre et rendue dans une réponse HTTP : y mettre
            # l'adresse annulerait le condensé posé partout ailleurs. La clé
            # d'idempotence suffit au support pour retrouver la ligne.
            raise ErreurEnvoi(
                "Adresse de courriel inexploitable pour l'envoi "
                f"« {message.cle_idempotence} ».")

        charge: dict[str, Any] = {
            "sender": {"email": self._expediteur, "name": self._nom},
            "to": [{"email": adresse, **(
                {"name": message.destinataire.nom} if message.destinataire.nom else {})}],
            "subject": _une_ligne(message.objet)[:300],
            "textContent": message.corps,
            # Deux en-têtes qui servent au support et à la conformité : l'un
            # rattache le message à la commande ou à la facture, l'autre permet au
            # destinataire de se désinscrire sans écrire à quelqu'un.
            # Les sauts de ligne sont retirés : cette valeur devient un en-tête
            # SMTP chez le fournisseur, et un retour à la ligne y ouvrirait un
            # en-tête de notre choix, une copie cachée par exemple.
            "headers": {"X-ADSUM-Cle": _une_ligne(message.cle_idempotence)[:128]},
            "tags": [message.categorie[:32]],
        }
        if message.corps_riche:
            charge["htmlContent"] = message.corps_riche

        reponse = self._transport.envoyer(
            "POST", f"{self._racine}/smtp/email",
            {"api-key": self._cle, "Content-Type": "application/json",
             "accept": "application/json"},
            json.dumps(charge).encode("utf-8"), DELAI_S)
        corps = reponse.json()

        if reponse.code >= 400:
            code_erreur = str(corps.get("code") or "")
            raison = str(corps.get("message") or "raison non précisée")[:300]
            if code_erreur in DEFINITIFS:
                raise DestinataireBloque(
                    f"Brevo refuse définitivement cette adresse : {raison}")
            raise ErreurEnvoi(f"Brevo a refusé l'envoi : {raison}")

        return Envoi(
            cle_idempotence=message.cle_idempotence,
            # Accepté et non remis : Brevo confirme la prise en charge, pas l'arrivée.
            # Dire « remis » ici rendrait indémontrable une réclamation légitime.
            statut=Statut.ACCEPTE,
            fournisseur=self.code,
            canal=Canal.COURRIEL,
            reference=str(corps.get("messageId") or ""),
            envoye_le=datetime.now(timezone.utc),
        )

    # -- Accusés -------------------------------------------------------------

    def verifier_accuse(self, entetes: dict[str, str], corps: bytes) -> AccuseReception:
        """Authentifier un retour de Brevo par le secret convenu dans l'URL.

        Brevo ne signe pas ses notifications. Le seul moyen de les authentifier est
        de ne publier l'adresse de réception qu'avec un secret dedans, et de vérifier
        ce secret. C'est faible, et c'est pourquoi un accusé ne fait jamais que
        renseigner un statut : il n'ouvre aucun droit et ne déclenche aucune dépense.
        """
        if not self._secret_accuse:
            raise ErreurEnvoi(
                "Aucun secret d'accusé configuré pour Brevo : les retours ne peuvent "
                "pas être authentifiés, donc ils sont refusés.")
        import hmac

        fourni = next((v for k, v in entetes.items()
                       if k.lower() in ("x-adsum-accuse", "x-mailin-secret")), "")
        # Garde ASCII avant la comparaison : sans elle, un octet non ASCII dans
        # l'en-tête lève TypeError et sort en 500 au lieu d'un refus tracé.
        if not fourni.isascii() or not hmac.compare_digest(fourni, self._secret_accuse):
            raise ErreurEnvoi("Accusé Brevo non authentifié.")

        try:
            charge = json.loads(corps.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ErreurEnvoi("Accusé Brevo illisible.") from e

        evenement = str(charge.get("event") or "").lower()
        return AccuseReception(
            reference=str(charge.get("message-id") or charge.get("messageId") or ""),
            statut=EVENEMENTS.get(evenement, Statut.ACCEPTE),
            fournisseur=self.code,
            identifiant_evenement=str(charge.get("id") or charge.get("ts_event") or ""),
            recu_le=datetime.now(timezone.utc),
            motif=charge.get("reason"),
            adresse=str(charge.get("email") or ""),
        )

    def sante(self) -> bool:
        reponse = self._transport.envoyer(
            "GET", f"{self._racine}/account",
            {"api-key": self._cle, "accept": "application/json"}, None, DELAI_S)
        return reponse.reussie
