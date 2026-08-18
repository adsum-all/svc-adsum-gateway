"""Telegram : le canal qui arrive vraiment.

En Afrique de l'Ouest, un courriel de relance atterrit dans un onglet que personne
n'ouvre, quand l'adresse existe. Telegram est lu. C'est pour cela qu'il n'est pas un
canal de secours ici mais un canal de plein droit.

Trois particularités.

**On ne peut écrire qu'à qui a écrit le premier.** Telegram interdit à un robot
d'ouvrir une conversation ; c'est la personne qui doit le démarrer. L'identifiant de
conversation est donc une preuve de consentement, et non une adresse que l'on
devine. Un robot bloqué par son destinataire remonte comme « bloqué » et non comme
un échec à réessayer.

**La mise en forme mord.** Le mode « MarkdownV2 » exige d'échapper une quinzaine de
caractères, dont le point et le tiret. Un montant « 15 000,00 » ou une date passent
sans problème, mais un motif de refus recopié d'un fournisseur casse le message
entier, qui n'arrive pas du tout. Cet adaptateur envoie donc du texte simple : ce
qui compte est que le message arrive, pas qu'il soit en gras.

**La limite est de quatre mille quatre-vingt-seize caractères.** Au-delà, Telegram
refuse tout. Le message est coupé proprement avec une mention, plutôt que perdu.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..port import (
    Canal,
    DestinataireBloque,
    Envoi,
    ErreurEnvoi,
    Fournisseur,
    Message,
    Statut,
)
from ..transport import Transport, TransportHttpx

API = "https://api.telegram.org"

#: La limite dure de Telegram. Un message plus long est refusé en entier, pas tronqué.
LONGUEUR_MAX = 4096
SUITE = "\n\n[Message trop long, la suite est dans votre espace ADSUM.]"

#: Les descriptions que Telegram rend quand réessayer ne servira jamais : la
#: personne a bloqué le robot, supprimé son compte, ou la conversation n'existe pas.
DEFINITIFS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
)

DELAI_S = 20.0


class Telegram(Fournisseur):
    """Messages instantanés par un robot Telegram."""

    code = "telegram"
    libelle = "Telegram"
    canaux = (Canal.TELEGRAM,)

    def __init__(
        self, jeton_robot: str, transport: Transport | None = None, racine: str = API,
    ) -> None:
        if not jeton_robot:
            raise ValueError(
                "Telegram exige le jeton du robot. Sans lui, aucun message ne part "
                "et l'échec ne se voit qu'à la première relance non reçue.")
        self._jeton = jeton_robot
        self._transport = transport or TransportHttpx()
        self._racine = racine.rstrip("/")

    def envoyer(self, message: Message) -> Envoi:
        if message.canal is not Canal.TELEGRAM:
            raise ErreurEnvoi(f"Telegram ne sert pas « {message.canal.value} ».")

        conversation = message.destinataire.adresse.strip()
        if not conversation:
            raise ErreurEnvoi("Aucun identifiant de conversation Telegram.")

        # L'objet et le corps sont recollés : Telegram n'a pas de sujet, et le perdre
        # laisse un message dont on ne sait pas de quoi il parle.
        texte = f"{message.objet}\n\n{message.corps}" if message.objet else message.corps
        if len(texte) > LONGUEUR_MAX:
            texte = texte[: LONGUEUR_MAX - len(SUITE)] + SUITE

        charge = {
            "chat_id": conversation,
            "text": texte,
            # Pas de mise en forme : un caractère non échappé ferait échouer tout le
            # message, et un motif de refus recopié d'un fournisseur en contient.
            "disable_web_page_preview": True,
        }
        reponse = self._transport.envoyer(
            "POST", f"{self._racine}/bot{self._jeton}/sendMessage",
            {"Content-Type": "application/json"},
            json.dumps(charge).encode("utf-8"), DELAI_S)
        corps = reponse.json()

        if reponse.code >= 400 or not corps.get("ok"):
            raison = str(corps.get("description") or "raison non précisée")
            if any(motif in raison.lower() for motif in DEFINITIFS):
                raise DestinataireBloque(
                    f"Telegram ne peut plus joindre ce destinataire : {raison[:200]}")
            raise ErreurEnvoi(f"Telegram a refusé l'envoi : {raison[:300]}")

        resultat = corps.get("result") or {}
        return Envoi(
            cle_idempotence=message.cle_idempotence,
            # Telegram remet immédiatement ou refuse : il n'y a pas d'état
            # intermédiaire comme pour un courriel.
            statut=Statut.REMIS,
            fournisseur=self.code,
            canal=Canal.TELEGRAM,
            reference=str(resultat.get("message_id") or ""),
            envoye_le=datetime.now(timezone.utc),
        )

    def sante(self) -> bool:
        reponse = self._transport.envoyer(
            "GET", f"{self._racine}/bot{self._jeton}/getMe", {}, None, DELAI_S)
        return reponse.reussie and bool(reponse.json().get("ok"))
