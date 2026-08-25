"""Le contrat que tout canal sortant doit satisfaire.

La passerelle est la seule porte par laquelle ADSUM parle au monde : courriel,
message instantané, SMS, notification poussée. Rien d'autre dans la plateforme
n'appelle un fournisseur externe directement.

Ce n'est pas de la symétrie pour la symétrie. Quatre choses en découlent, et chacune
coûte cher quand elle manque.

**Un seul endroit change quand un fournisseur change.** Une adresse d'expédition
recopiée dans trois services reste sur l'ancienne valeur dans l'un des trois le jour
de la bascule, et les messages partent en indésirables sans que personne ne le voie.

**Un message envoyé deux fois n'est envoyé qu'une fois.** Un délai dépassé est
indiscernable d'un échec : l'appelant réessaie, et le destinataire reçoit deux fois
la même mise en demeure. La clé d'idempotence est portée par le contrat, pas laissée
au bon vouloir de l'appelant.

**Un canal en panne ne fait pas taire la plateforme.** Les fournisseurs sont classés,
et l'échec de l'un fait passer au suivant. Un service qui n'a qu'une route se tait
entièrement le jour où elle tombe.

**Rien ne part sans trace.** Ce qui a été envoyé, à qui, par quel fournisseur et avec
quel résultat. Sans ce registre, une réclamation « je n'ai rien reçu » est
indémontrable dans les deux sens.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Canal(str, Enum):
    """Par quoi le message part. Le choix appartient à l'appelant, pas au hasard."""

    COURRIEL = "courriel"
    TELEGRAM = "telegram"
    #: Messagerie WhatsApp par le compte professionnel de l'éditeur. Le seul
    #: canal dont le fournisseur impose ce que l'on a le droit d'écrire : hors
    #: fenêtre de réponse, un message non sollicité doit passer par un gabarit
    #: approuvé par Meta. L'adaptateur porte cette contrainte, pas l'appelant.
    WHATSAPP = "whatsapp"
    SMS = "sms"
    #: Notification poussée vers une application mobile installée.
    POUSSEE = "poussee"


class Statut(str, Enum):
    """Où en est un envoi. Les trois derniers états sont définitifs."""

    EN_ATTENTE = "en_attente"
    #: Accepté par le fournisseur, pas encore remis. La grande majorité des envois
    #: s'arrêtent ici : peu de fournisseurs confirment la remise réelle.
    ACCEPTE = "accepte"
    REMIS = "remis"
    REFUSE = "refuse"
    #: Le destinataire a demandé à ne plus rien recevoir, ou l'adresse est morte.
    #: Distinct d'un refus : réessayer ne servira jamais.
    BLOQUE = "bloque"

    @property
    def terminal(self) -> bool:
        return self in (Statut.REMIS, Statut.REFUSE, Statut.BLOQUE)


class ErreurEnvoi(Exception):
    """Un refus explicable. Ne porte jamais d'identifiant de fournisseur."""


class CanalNonServi(ErreurEnvoi):
    """Aucun fournisseur configuré ne sert ce canal."""


class DestinataireBloque(ErreurEnvoi):
    """Le destinataire s'est désabonné ou son adresse est morte.

    Une exception à part, parce que la conduite à tenir est l'inverse de celle d'un
    échec : ne pas réessayer, et retirer l'adresse de la liste.
    """


@dataclass(frozen=True)
class Destinataire:
    """À qui l'on écrit, et rien de plus.

    Une adresse, un identifiant de conversation ou un numéro selon le canal. Aucune
    donnée personnelle au-delà : la passerelle achemine, elle ne constitue pas un
    annuaire. Le nom est optionnel et sert seulement à l'en-tête d'un courriel.
    """

    #: Adresse, identifiant Telegram, numéro au format international, ou jeton
    #: d'appareil selon le canal. Le format est vérifié par l'adaptateur, qui seul
    #: sait ce que son fournisseur accepte.
    adresse: str
    nom: str | None = None
    #: Deux lettres ISO 639-1. Sert à choisir la version du gabarit, pas à traduire
    #: à la volée : une traduction automatique d'une mise en demeure est un risque.
    langue: str = "fr"


@dataclass(frozen=True)
class Message:
    """Ce qu'on demande à la passerelle d'acheminer."""

    canal: Canal
    destinataire: Destinataire
    #: Vide pour les canaux qui n'ont pas de sujet, Telegram et SMS notamment.
    objet: str
    corps: str
    #: Stable d'un essai à l'autre. Deux appels avec la même clé produisent un seul
    #: envoi, et le second rend le résultat du premier.
    cle_idempotence: str
    #: Version enrichie du corps, quand le canal la porte. Jamais obligatoire : un
    #: message qui n'existe qu'en HTML est illisible pour qui refuse le HTML.
    corps_riche: str | None = None
    #: Qui envoie, du point de vue du métier. Sert au classement dans le journal et
    #: à la mesure : les relances impayées et les invitations n'ont pas le même
    #: enjeu quand un canal se dégrade.
    categorie: str = "transactionnel"
    #: L'organisation concernée, quand il y en a une. Le journal en a besoin pour
    #: qu'un exploitant puisse répondre à « qu'avons-nous envoyé à ce client ».
    organisation: str = ""
    metadonnees: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Envoi:
    """Ce que la passerelle a fait du message."""

    cle_idempotence: str
    statut: Statut
    fournisseur: str
    canal: Canal
    #: L'identifiant du fournisseur, quand il en donne un. Seul moyen de rapprocher
    #: un accusé de remise reçu plus tard.
    reference: str = ""
    envoye_le: datetime | None = None
    #: Le motif du fournisseur, gardé tel quel. Le reformuler prive le support de ce
    #: qui lui permet de comprendre un refus.
    motif: str | None = None
    #: Vrai quand cet envoi avait déjà été fait et que rien n'est reparti.
    deja_fait: bool = False


@dataclass(frozen=True)
class AccuseReception:
    """Un retour de fournisseur : remise, ouverture, refus, désabonnement.

    Produit seulement après vérification de la signature. Un accusé non vérifié
    permettrait à n'importe qui de marquer une adresse comme bloquée, donc de faire
    taire la plateforme pour un destinataire choisi.
    """

    reference: str
    statut: Statut
    fournisseur: str
    #: L'identifiant de l'événement chez le fournisseur, pour la déduplication.
    identifiant_evenement: str = ""
    recu_le: datetime | None = None
    motif: str | None = None
    #: Renseignée quand le retour concerne une adresse plutôt qu'un envoi précis,
    #: ce qui est le cas des désabonnements et des adresses mortes.
    adresse: str = ""
    brut: dict[str, Any] = field(default_factory=dict)


class Fournisseur(ABC):
    """Un fournisseur sortant. Un service de courriel et un robot se ressemblent ici."""

    #: Identifiant stable, écrit dans le journal. Le renommer orpheline l'historique.
    code: str
    libelle: str
    #: Les canaux que cet adaptateur sert réellement. En déclarer un de plus fait
    #: échouer au moment de l'envoi un message que le registre lui aura confié.
    canaux: tuple[Canal, ...]

    @abstractmethod
    def envoyer(self, message: Message) -> Envoi:
        """Remettre le message au fournisseur.

        Lève ErreurEnvoi si le fournisseur refuse ou ne répond pas ; l'appelant
        essaiera le suivant. Lève DestinataireBloque quand réessayer est inutile.
        """

    def verifier_accuse(self, entetes: dict[str, str], corps: bytes) -> AccuseReception:
        """Authentifier un retour de fournisseur, puis le traduire.

        Pas abstraite : beaucoup de fournisseurs n'en envoient aucun, et forcer
        chaque adaptateur à écrire un refus ajoute du bruit. Le défaut refuse.
        """
        raise ErreurEnvoi(f"{self.libelle} n'envoie pas d'accusé de réception")

    def sante(self) -> bool:
        """Le fournisseur répond-il ? Interrogé par la sonde, jamais dans un envoi.

        Le défaut est vrai : un fournisseur qui n'expose pas de sonde n'est pas en
        panne pour autant, et le déclarer mort le retirerait du classement sans
        raison.
        """
        return True
