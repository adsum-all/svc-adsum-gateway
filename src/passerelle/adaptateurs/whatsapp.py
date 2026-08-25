"""WhatsApp par l'API Cloud de Meta.

Cet adaptateur existe pour une raison précise : WhatsApp était jusqu'ici appelé
directement depuis l'API métier, sans passer par la passerelle. Un canal hors
passerelle n'a ni repli, ni journal commun, ni registre d'envoi, ni vérification de
signature sur les retours. Le ramener ici lui donne les quatre.

WhatsApp se distingue des autres canaux sur un point qui commande tout le fichier :
le fournisseur impose ce que l'on a le droit d'écrire. Hors de la fenêtre de vingt
quatre heures qui suit un message du destinataire, Meta refuse tout texte libre et
n'accepte qu'un gabarit approuvé à l'avance. Un appelant qui l'ignore voit ses envois
refusés avec le code 131047, et le refus ne dit rien d'utile tant qu'on ne connaît pas
la règle. L'adaptateur porte donc la contrainte : il sait choisir un gabarit, il sait
mettre le texte en forme pour qu'un gabarit l'accepte, et il traduit le refus en une
phrase qui dit quoi faire.

Deux pièges de mise en forme sont traités ici parce qu'ils ne se découvrent
autrement qu'en production. Un paramètre de gabarit ne supporte ni saut de ligne, ni
tabulation, ni quatre espaces consécutifs : Meta rejette le message entier, pas le
seul paramètre. Et la longueur admise n'est pas la même pour un texte libre et pour un
paramètre de gabarit, quatre mille quatre-vingt-seize contre mille vingt-quatre.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
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

API = "https://graph.facebook.com"
#: Une version figée, pas la plus récente disponible. Suivre automatiquement la
#: dernière version d'une API tierce fait changer le comportement sans qu'aucun
#: commit ne l'explique.
VERSION = "v21.0"
DELAI_S = 25.0

LONGUEUR_TEXTE = 4096
LONGUEUR_PARAMETRE = 1024
SUITE = " [...]"

#: Les codes Meta pour lesquels réessayer ne sert jamais, ici comme ailleurs : le
#: numéro n'a pas de compte WhatsApp, ou il n'existe pas. Le code 131047, hors
#: fenêtre de réponse, n'y figure pas volontairement : il se répare en passant par un
#: gabarit, et marquer le destinataire comme bloqué le priverait de tout envoi futur
#: à cause d'une erreur qui nous appartient.
DEFINITIFS = frozenset({131026, 133010})

#: Ce que Meta refuse dans un paramètre de gabarit.
BLANCS_INTERDITS = re.compile(r"[\r\n\t]+|\s{4,}")
NON_CHIFFRE = re.compile(r"[^0-9]")

#: Les refus qui viennent du gabarit lui-même : nom inconnu, langue absente, version
#: non approuvée, ou nombre de valeurs qui ne correspond pas aux emplacements.
CODES_GABARIT = frozenset({132000, 132001, 132005, 132007, 132012, 132015, 132016,
                           132068})
#: Le jeton n'est plus valable. Un jeton permanent se révoque au changement de mot de
#: passe du compte Meta, sans que rien ne l'annonce.
CODES_JETON = frozenset({190, 102, 463})
#: Le compte expéditeur est bridé pour l'instant. Réessayer plus tard sert.
CODES_DEBIT = frozenset({4, 80007, 130429, 131048})


class WhatsApp(Fournisseur):
    """Messages WhatsApp par le compte professionnel de l'éditeur."""

    code = "whatsapp"
    libelle = "WhatsApp Business"
    canaux = (Canal.WHATSAPP,)

    def __init__(
        self,
        jeton: str,
        identifiant_numero: str,
        secret_application: str = "",
        gabarit_defaut: str = "",
        langue_defaut: str = "fr",
        transport: Transport | None = None,
        racine: str = API,
        version: str = VERSION,
    ) -> None:
        if not jeton:
            raise ValueError(
                "WhatsApp exige un jeton d'accès permanent. Un jeton temporaire "
                "expire au bout de vingt-quatre heures et le canal se tait sans "
                "qu'aucune alerte ne le dise.")
        if not identifiant_numero:
            raise ValueError(
                "WhatsApp exige l'identifiant du numéro expéditeur. Ce n'est pas le "
                "numéro lui-même : c'est l'identifiant que Meta lui attribue.")
        self._jeton = jeton
        self._numero_expediteur = identifiant_numero
        self._secret = secret_application
        self._gabarit_defaut = gabarit_defaut
        self._langue_defaut = langue_defaut or "fr"
        self._transport = transport or TransportHttpx()
        self._racine = racine.rstrip("/")
        self._version = version

    # ------------------------------------------------------------------ envoi

    def envoyer(self, message: Message) -> Envoi:
        if message.canal is not Canal.WHATSAPP:
            raise ErreurEnvoi("WhatsApp ne sert pas le canal demandé.")

        charge: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _numero(message.destinataire.adresse),
        }
        charge.update(self._contenu(message))

        reponse = self._transport.envoyer(
            "POST",
            f"{self._racine}/{self._version}/{self._numero_expediteur}/messages",
            {
                "Authorization": f"Bearer {self._jeton}",
                "Content-Type": "application/json",
            },
            json.dumps(charge, ensure_ascii=False).encode("utf-8"),
            DELAI_S,
        )
        corps = reponse.json()

        if reponse.code >= 400 or "error" in corps:
            self._lever(corps, reponse.code)

        envoyes = corps.get("messages") or []
        reference = str(envoyes[0].get("id", "")) if envoyes else ""
        return Envoi(
            cle_idempotence=message.cle_idempotence,
            # Accepté, pas remis. Meta prend le message en charge puis confirme la
            # remise par un retour séparé, parfois plusieurs minutes après. Écrire
            # « remis » ici ferait mentir le journal sur le seul point qu'on lui
            # demande.
            statut=Statut.ACCEPTE,
            fournisseur=self.code,
            canal=Canal.WHATSAPP,
            reference=reference,
            envoye_le=datetime.now(timezone.utc),
        )

    def _contenu(self, message: Message) -> dict[str, Any]:
        """Le corps du message, en gabarit quand il en faut un.

        Le gabarit se choisit dans cet ordre : celui que l'appelant nomme dans les
        métadonnées, sinon celui configuré par défaut, sinon rien et le message part
        en texte libre. Le texte libre ne passe que dans la fenêtre de réponse, ce
        que l'adaptateur ne peut pas savoir : c'est Meta qui tranche, et le refus est
        traduit plus bas en une phrase qui dit quoi faire.
        """
        nom = message.metadonnees.get("gabarit") or self._gabarit_defaut
        if not nom:
            return {"type": "text", "text": {
                "preview_url": False,
                "body": _borner(_texte_complet(message), LONGUEUR_TEXTE),
            }}

        langue = (
            message.metadonnees.get("gabarit_langue")
            or message.destinataire.langue
            or self._langue_defaut
        )
        gabarit: dict[str, Any] = {"name": nom, "language": {"code": langue}}
        parametres = self._parametres(message)
        if parametres:
            gabarit["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in parametres],
            }]
        return {"type": "template", "template": gabarit}

    def _parametres(self, message: Message) -> list[str]:
        """Les valeurs à insérer dans le gabarit, dans l'ordre.

        Elles se déclarent gabarit_parametre_1, gabarit_parametre_2, et ainsi de
        suite. La lecture s'arrête au premier rang absent : un trou dans la
        numérotation décalerait toutes les valeurs suivantes, et le message partirait
        avec un montant à la place d'une date sans que rien n'échoue.

        Sans aucun rang déclaré, le corps du message sert de valeur unique. C'est le
        cas courant d'un gabarit ADSUM qui ne contient qu'un emplacement.
        """
        valeurs: list[str] = []
        rang = 1
        while True:
            valeur = message.metadonnees.get(f"gabarit_parametre_{rang}")
            if valeur is None:
                break
            valeurs.append(_aplatir(valeur))
            rang += 1
        if valeurs:
            return valeurs
        corps = _texte_complet(message)
        return [_aplatir(corps)] if corps else []

    def _lever(self, corps: dict[str, Any], code_http: int) -> None:
        """Traduire un refus de Meta, sans jamais rendre la main."""
        erreur = corps.get("error") or {}
        donnees = erreur.get("error_data") or {}
        code = _entier(erreur.get("code"))
        raison = str(
            donnees.get("details") or erreur.get("message")
            or f"refus HTTP {code_http}"
        )[:300]

        if code in DEFINITIFS:
            raise DestinataireBloque(
                f"WhatsApp ne peut pas joindre ce numéro : {raison}")
        if code == 131047:
            raise ErreurEnvoi(
                "WhatsApp refuse un texte libre hors de la fenêtre de vingt-quatre "
                "heures. Ce message doit passer par un gabarit approuvé : nommer le "
                "gabarit dans la métadonnée « gabarit », ou en configurer un par "
                f"défaut. Refus du fournisseur : {raison}")
        if code in CODES_GABARIT:
            raise ErreurEnvoi(
                "WhatsApp refuse le gabarit : il n'existe pas sous ce nom et cette "
                "langue, il n'est pas approuvé, ou le nombre de valeurs fournies ne "
                f"correspond pas à ses emplacements. Refus du fournisseur : {raison}")
        if code in CODES_JETON:
            raise ErreurEnvoi(
                "Le jeton WhatsApp n'est plus valable. Un jeton permanent se révoque "
                "au changement de mot de passe du compte Meta, et le canal se tait "
                f"jusqu'à son remplacement. Refus du fournisseur : {raison}")
        if code in CODES_DEBIT:
            raise ErreurEnvoi(
                "WhatsApp limite le débit du compte expéditeur pour l'instant. "
                f"Réessayer plus tard reste utile. Refus du fournisseur : {raison}")
        precision = f" (code {code})" if code else ""
        raise ErreurEnvoi(f"WhatsApp a refusé l'envoi : {raison}{precision}")

    # ---------------------------------------------------------------- accusés

    def verifier_accuse(self, entetes: dict[str, str], corps: bytes) -> AccuseReception:
        """Authentifier un retour de Meta, puis le traduire.

        Sans secret d'application configuré, l'accusé est refusé plutôt qu'accepté à
        l'aveugle. Un retour non vérifié permettrait à quiconque connaît l'adresse du
        service de déclarer un numéro mort, donc de faire taire la plateforme pour un
        destinataire choisi.
        """
        if not self._secret:
            raise ErreurEnvoi(
                "Aucun secret d'application WhatsApp n'est configuré : les accusés "
                "ne peuvent pas être authentifiés, ils sont donc refusés.")

        minuscules = {c.lower(): v for c, v in entetes.items()}
        presentee = minuscules.get("x-hub-signature-256", "")
        if not presentee.startswith("sha256="):
            raise ErreurEnvoi("Signature WhatsApp absente ou mal formée.")
        attendue = hmac.new(
            self._secret.encode("utf-8"), corps, hashlib.sha256).hexdigest()
        # Comparaison à temps constant. Un test d'égalité ordinaire laisse fuir la
        # signature attendue caractère par caractère, à force de mesures.
        if not hmac.compare_digest(presentee[len("sha256="):], attendue):
            raise ErreurEnvoi("Signature WhatsApp invalide : accusé refusé.")

        try:
            charge = json.loads(corps.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as echec:
            raise ErreurEnvoi("Accusé WhatsApp illisible.") from echec

        statut_brut = _premier_statut(charge)
        if statut_brut is None:
            raise ErreurEnvoi(
                "Ce retour WhatsApp ne porte aucun changement d'état d'envoi. Les "
                "messages entrants ne sont pas des accusés et n'ont rien à faire "
                "ici.")

        erreurs = statut_brut.get("errors") or []
        code_erreur = _entier(erreurs[0].get("code")) if erreurs else 0
        motif = str(erreurs[0].get("title") or "")[:300] if erreurs else None

        return AccuseReception(
            reference=str(statut_brut.get("id", "")),
            statut=_traduire(str(statut_brut.get("status", "")), code_erreur),
            fournisseur=self.code,
            identifiant_evenement=(
                f"{statut_brut.get('id', '')}:{statut_brut.get('status', '')}"),
            recu_le=_horodatage(statut_brut.get("timestamp")),
            motif=motif,
            adresse=str(statut_brut.get("recipient_id", "")),
            brut=statut_brut,
        )

    def sante(self) -> bool:
        reponse = self._transport.envoyer(
            "GET",
            f"{self._racine}/{self._version}/{self._numero_expediteur}"
            "?fields=verified_name",
            {"Authorization": f"Bearer {self._jeton}"},
            None,
            DELAI_S,
        )
        return reponse.reussie


# --------------------------------------------------------------------- outils


def _numero(adresse: str) -> str:
    """Le numéro au format attendu par Meta : chiffres seuls, indicatif compris.

    Un numéro national, sans indicatif, est accepté par l'API et remis à un inconnu
    dans un autre pays. Le refuser ici est le seul endroit où l'erreur reste visible.
    """
    chiffres = NON_CHIFFRE.sub("", adresse or "")
    if len(chiffres) < 8:
        raise ErreurEnvoi(
            "Numéro WhatsApp inutilisable : il faut un numéro au format "
            "international, indicatif pays compris.")
    return chiffres


def _texte_complet(message: Message) -> str:
    """L'objet et le corps recollés.

    WhatsApp n'a pas de sujet, et le perdre laisse un message dont on ne sait pas de
    quoi il parle.
    """
    if message.objet and message.corps:
        return f"*{message.objet}*\n\n{message.corps}"
    return message.corps or message.objet


def _aplatir(valeur: str) -> str:
    """Rendre une valeur acceptable dans un gabarit.

    Meta refuse le message entier, pas seulement le paramètre, dès qu'une valeur
    contient un saut de ligne, une tabulation ou quatre espaces consécutifs.
    """
    return _borner(BLANCS_INTERDITS.sub(" ", valeur).strip(), LONGUEUR_PARAMETRE)


def _borner(texte: str, limite: int) -> str:
    if len(texte) <= limite:
        return texte
    return texte[: limite - len(SUITE)] + SUITE


def _entier(valeur: Any) -> int:
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return 0


def _premier_statut(charge: dict[str, Any]) -> dict[str, Any] | None:
    for entree in charge.get("entry") or []:
        for changement in entree.get("changes") or []:
            statuts = (changement.get("value") or {}).get("statuses") or []
            if statuts:
                return dict(statuts[0])
    return None


def _traduire(statut: str, code_erreur: int) -> Statut:
    if statut in ("delivered", "read"):
        return Statut.REMIS
    if statut == "sent":
        return Statut.ACCEPTE
    if statut == "failed":
        return Statut.BLOQUE if code_erreur in DEFINITIFS else Statut.REFUSE
    return Statut.EN_ATTENTE


def _horodatage(valeur: Any) -> datetime | None:
    """Meta date en secondes depuis l'époque, en texte."""
    secondes = _entier(valeur)
    if not secondes:
        return None
    return datetime.fromtimestamp(secondes, tz=timezone.utc)
