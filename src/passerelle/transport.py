"""Comment un adaptateur parle à son fournisseur.

Le transport est injecté plutôt qu'importé, pour trois raisons qui ont chacune un
coût quand on l'ignore.

**Un adaptateur qui construit lui-même son client HTTP ne se teste pas** sans réseau,
donc il n'est pas testé, donc ses erreurs se découvrent en production, un jour de
paiement.

**Les délais sont une décision d'exploitation, pas de code.** Un fournisseur de
mobile money répond en trente secondes parce qu'il attend que le payeur décroche ;
un fournisseur de carte répond en deux. Un délai codé en dur convient à l'un et fait
échouer l'autre.

**Une trace de requête ne doit jamais contenir de secret.** Le transport est le seul
endroit qui voit les en-têtes d'authentification, donc le seul endroit où les
masquer. Le faire dans chaque adaptateur, c'est l'oublier dans l'un d'eux.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Reponse:
    code: int
    corps: bytes

    @property
    def reussie(self) -> bool:
        return 200 <= self.code < 300

    def json(self) -> dict[str, Any]:
        if not self.corps:
            return {}
        try:
            charge = json.loads(self.corps.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return charge if isinstance(charge, dict) else {"donnees": charge}


class Transport(Protocol):
    """Le strict nécessaire pour parler à une API de paiement."""

    def envoyer(
        self, methode: str, url: str, entetes: dict[str, str],
        corps: bytes | None = None, delai: float = 30.0,
    ) -> Reponse:
        ...


#: Les en-têtes qui portent un secret. Jamais journalisés en clair : une trace de
#: requête finit dans un agrégateur de logs, qui n'est pas un coffre.
ENTETES_SENSIBLES = frozenset({
    "authorization", "x-api-key", "x-secret-key", "apikey", "x-token",
    "stripe-signature", "verif-hash", "x-paystack-signature", "secret-key",
    # Avec le tiret : c'est exactement l'en-tête où Brevo attend sa clé d'API,
    # et son absence d'ici la laissait passer en clair dans les traces. Les
    # trois graphies sont donc listées plutôt que devinées.
    "api-key", "x-adsum-accuse", "x-mailin-secret", "wave-signature",
    "paypal-transmission-sig",
})


def masquer_url(url: str) -> str:
    """Une adresse prête à être tracée, secrets retirés du chemin.

    Certains fournisseurs portent leur identifiant dans l'URL et non dans un
    en-tête : un robot Telegram s'appelle « /bot<jeton>/sendMessage ». Masquer les
    seuls en-têtes laissait donc passer ce jeton en clair, alors que cette classe
    se décrit comme ne jamais écrire de secret.
    """
    import re

    propre = re.sub(r"/bot[^/]+/", "/bot***/", url)
    # Ce qui suit un point d'interrogation porte souvent une clé chez les
    # fournisseurs qui n'utilisent pas d'en-tête d'autorisation.
    return propre.split("?")[0]


def masquer(entetes: dict[str, str]) -> dict[str, str]:
    """Les mêmes en-têtes, prêts à être tracés."""
    return {
        cle: ("***" if cle.lower() in ENTETES_SENSIBLES else valeur)
        for cle, valeur in entetes.items()
    }


class TransportHttpx:
    """Le transport de production, sur httpx.

    Importé à l'appel et non au chargement du module : le reste du paquet, dont les
    règles de routage et de signature, doit pouvoir être importé et testé sans que
    httpx soit installé.
    """

    def __init__(self, delai_defaut: float = 30.0) -> None:
        self.delai_defaut = delai_defaut

    def envoyer(
        self, methode: str, url: str, entetes: dict[str, str],
        corps: bytes | None = None, delai: float | None = None,
    ) -> Reponse:
        import httpx

        reponse = httpx.request(
            methode, url, headers=entetes, content=corps,
            timeout=delai or self.delai_defaut,
        )
        return Reponse(code=reponse.status_code, corps=reponse.content)


@dataclass
class TransportEnregistre:
    """Un transport qui note ce qu'il envoie, pour l'exploitation.

    Ce n'est pas un test double : il enveloppe un transport réel et sert en
    production à tracer les échanges sans jamais écrire de secret. Les traces sont
    ce qui permet de réconcilier un paiement dont le webhook s'est perdu.
    """

    interne: Transport
    traces: list[dict[str, Any]] = field(default_factory=list)
    #: Au-delà, la trace est tronquée : un corps de réponse peut porter des données
    #: personnelles que rien n'oblige à conserver.
    taille_trace: int = 2000

    def envoyer(
        self, methode: str, url: str, entetes: dict[str, str],
        corps: bytes | None = None, delai: float = 30.0,
    ) -> Reponse:
        reponse = self.interne.envoyer(methode, url, entetes, corps, delai)
        self.traces.append({
            "methode": methode,
            "url": masquer_url(url),
            "entetes": masquer(entetes),
            "code": reponse.code,
            "extrait": reponse.corps[: self.taille_trace].decode("utf-8", "replace"),
        })
        return reponse
