"""Voir ce que fait la passerelle, sans voir ce qu'il ne faut pas.

Copie assumée de la couche du service commerce : les deux services sont des
dépôts séparés et il n'existe pas de registre de paquets Python interne. Le nom
du service est un paramètre et non une constante, pour que les deux copies ne
puissent diverger que sur cette ligne.

L'enjeu est particulier ici : la passerelle voit passer toutes les adresses de
la plateforme. Une trace bavarde y ferait de l'agrégateur de journaux le plus
gros fichier d'adresses de l'entreprise.

Le service commerce n'avait aucune trace. Une panne en production y était invisible :
le seul signal était un client qui écrit pour dire que son module n'est pas arrivé,
et il n'y avait alors rien à lire pour comprendre.

Quatre décisions, chacune contre un défaut courant des couches de traces.

**Les traces sont du JSON, une ligne par événement.** Un message en texte libre se
lit à l'oeil sur une machine, et ne se requête pas dans un agrégateur. Le jour où
l'on cherche « toutes les requêtes en erreur de cette organisation depuis une heure »,
le texte libre ne répond pas.

**Chaque requête porte un identifiant de corrélation**, rendu dans l'en-tête de la
réponse. Sans lui, les lignes d'une même requête sont éparpillées et rien ne les
relie. L'identifiant vient de l'appelant s'il en fournit un, ce qui permet de suivre
une action à travers le portail, le commerce et la passerelle.

**Aucune donnée personnelle, aucun secret.** Pas de corps de requête, pas d'adresse
de courriel, pas d'en-tête d'autorisation. Un agrégateur de traces n'est pas un
coffre, et une trace vit plus longtemps que la donnée qu'elle contient. L'organisation
est identifiée, jamais la personne.

**Les mesures sont des compteurs et des paliers, pas des échantillons.** Garder
chaque durée en mémoire fait grossir le processus sans fin. Les paliers répondent à
la seule question utile en exploitation : quelle part des requêtes dépasse une
seconde.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

#: L'en-tête qui porte l'identifiant de corrélation. Le même nom des deux côtés,
#: sinon la chaîne se casse au premier saut entre services.
ENTETE_CORRELATION = "x-adsum-correlation"

#: Les en-têtes qui ne doivent jamais apparaître dans une trace. Une trace part dans
#: un agrégateur, qui n'est pas un coffre.
ENTETES_INTERDITS = frozenset({
    "authorization", "cookie", "set-cookie", "x-api-key",
    "x-mailin-secret", "x-adsum-accuse", "api-key",
})

#: Les paliers de durée, en secondes. Choisis sur ce qui se décide : au-delà d'une
#: seconde un écran paraît lent, au-delà de cinq l'utilisateur recharge, au-delà de
#: dix l'hébergeur coupe.
PALIERS_S = (0.1, 0.3, 1.0, 3.0, 5.0, 10.0)


class FormateurJson(logging.Formatter):
    """Une ligne de trace, en JSON, requêtable."""

    def __init__(self, service: str = "adsum-passerelle") -> None:
        super().__init__()
        self.service = service

    def format(self, enregistrement: logging.LogRecord) -> str:
        charge: dict[str, Any] = {
            "horodatage": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(enregistrement.created)) + "Z",
            "niveau": enregistrement.levelname,
            "service": self.service,
            "message": enregistrement.getMessage(),
        }
        # Les champs métier sont posés par l'appelant via `extra`. Filtrés sur ce que
        # le format d'un enregistrement standard ne contient pas déjà, pour ne pas
        # recopier tout l'état interne de la bibliothèque de journalisation.
        for cle, valeur in enregistrement.__dict__.items():
            if cle.startswith("adsum_"):
                charge[cle[len("adsum_"):]] = valeur
        if enregistrement.exc_info:
            # Le type et le message, jamais la pile complète : une pile contient les
            # valeurs des variables locales, dont des secrets.
            classe, erreur, _ = enregistrement.exc_info
            charge["erreur"] = f"{classe.__name__ if classe else '?'}: {erreur}"[:300]
        return json.dumps(charge, ensure_ascii=False)


def configurer(niveau: str = "INFO") -> logging.Logger:
    """Poser la journalisation. Idempotent : deux appels ne doublent pas les lignes."""
    journal = logging.getLogger("passerelle")
    journal.setLevel(niveau.upper())
    if not journal.handlers:
        sortie = logging.StreamHandler(sys.stdout)
        sortie.setFormatter(FormateurJson())
        journal.addHandler(sortie)
    # Sans cela, chaque ligne part aussi au journal racine et sort une seconde fois,
    # en texte cette fois, ce qui double la facture d'un agrégateur facturé au volume.
    journal.propagate = False
    return journal


@dataclass
class Mesures:
    """Les compteurs du service. Bornés en mémoire, sûrs entre fils.

    Bornés : la clé est la route déclarée, pas le chemin appelé. Compter par chemin
    laisserait un appelant faire grossir la table sans fin en variant l'URL, ce qui
    est une façon discrète d'épuiser la mémoire d'un service.
    """

    #: (route, méthode, statut) -> nombre
    requetes: dict[tuple[str, str, int], int] = field(
        default_factory=lambda: defaultdict(int))
    #: (route, palier) -> nombre de requêtes sous ce palier
    durees: dict[tuple[str, float], int] = field(
        default_factory=lambda: defaultdict(int))
    #: Somme des durées par route, pour la moyenne.
    somme_s: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    #: Événements métier nommés : paiements confirmés, relances parties, etc.
    evenements: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _verrou: Lock = field(default_factory=Lock, repr=False)

    #: Au-delà, on cesse d'ajouter des clés nouvelles. Un service qui s'étouffe en
    #: comptant ses requêtes est pire qu'un service sans mesures.
    plafond_cles: int = 500

    #: Sous quelle clé se rangent les routes une fois le plafond atteint. Une clé
    #: unique plutôt qu'un abandon : le trafic continue d'être compté, il cesse
    #: seulement d'être détaillé.
    AUTRE = "autre"

    def observer(self, route: str, methode: str, statut: int, duree_s: float) -> None:
        with self._verrou:
            # Les trois structures sont bornées, pas seulement la première. Deux
            # d'entre elles ne l'étaient pas : un appelant non authentifié faisait
            # croître la mémoire sans fin en variant l'URL, et la sortie des mesures
            # avec elle, ce qui cassait aussi la supervision par explosion de
            # cardinalité.
            #
            # Le test porte sur l'existence de la clé avant sa taille : sans cela,
            # une fois le plafond atteint, même les compteurs déjà connus cessaient
            # d'être incrémentés et les mesures gelaient en silence.
            cle = (route, methode, statut)
            if cle not in self.requetes and len(self.requetes) >= self.plafond_cles:
                route, cle = self.AUTRE, (self.AUTRE, methode, statut)
            elif route not in self.somme_s and len(self.somme_s) >= self.plafond_cles:
                route, cle = self.AUTRE, (self.AUTRE, methode, statut)

            self.requetes[cle] += 1
            for palier in PALIERS_S:
                if duree_s <= palier:
                    self.durees[(route, palier)] += 1
            self.somme_s[route] += duree_s

    def compter(self, evenement: str, combien: int = 1) -> None:
        """Un fait métier. « paiement_confirme », « module_deploye », « relance_partie »."""
        with self._verrou:
            if evenement in self.evenements or len(self.evenements) < self.plafond_cles:
                self.evenements[evenement] += combien

    def exposer(self) -> str:
        """Les mesures au format texte de Prometheus.

        Ce format plutôt qu'un JSON maison : il est lu tel quel par tous les outils
        de supervision, et n'oblige donc à écrire aucun adaptateur le jour où
        l'exploitation en branche un.
        """
        with self._verrou:
            lignes = [
                "# HELP adsum_requetes_total Requêtes servies par route, méthode et statut",
                "# TYPE adsum_requetes_total counter",
            ]
            for (route, methode, statut), n in sorted(self.requetes.items()):
                lignes.append(
                    f'adsum_requetes_total{{route="{_echapper(route)}",'
                    f'methode="{methode}",statut="{statut}"}} {n}')

            lignes += [
                "# HELP adsum_requete_duree_secondes Requêtes servies sous un palier",
                "# TYPE adsum_requete_duree_secondes histogram",
            ]
            for (route, palier), n in sorted(self.durees.items()):
                lignes.append(
                    f'adsum_requete_duree_secondes_bucket{{route="{_echapper(route)}",'
                    f'le="{palier}"}} {n}')
            for route, somme in sorted(self.somme_s.items()):
                lignes.append(
                    f'adsum_requete_duree_secondes_sum{{route="{_echapper(route)}"}} '
                    f'{somme:.4f}')

            lignes += [
                "# HELP adsum_evenements_total Faits métier comptés par le service",
                "# TYPE adsum_evenements_total counter",
            ]
            for nom, n in sorted(self.evenements.items()):
                lignes.append(f'adsum_evenements_total{{evenement="{_echapper(nom)}"}} {n}')

            return "\n".join(lignes) + "\n"

    def instantane(self) -> dict[str, Any]:
        """Les mêmes chiffres en JSON, pour un écran d'exploitation."""
        with self._verrou:
            par_statut: dict[str, int] = defaultdict(int)
            for (_, _, statut), n in self.requetes.items():
                par_statut[str(statut)] += n
            total = sum(par_statut.values())
            erreurs = sum(n for s, n in par_statut.items() if s.startswith("5"))
            return {
                "requetes": total,
                "par_statut": dict(sorted(par_statut.items())),
                # La part d'erreurs serveur est le seul chiffre qu'un exploitant
                # regarde en premier. Le calculer ici évite qu'un écran le fasse
                # faux.
                "part_erreurs_serveur": round(erreurs / total, 4) if total else 0.0,
                "evenements": dict(sorted(self.evenements.items())),
            }


def _echapper(valeur: str) -> str:
    """Protéger une étiquette Prometheus. Un guillemet non échappé casse la sortie."""
    return valeur.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:120]


class TraceRequetes:
    """Le passage obligé de toute requête : identifiant, durée, trace, mesure.

    Écrit en middleware ASGI brut plutôt qu'en dépendance : une dépendance ne voit
    ni les requêtes rejetées avant le routage, ni celles qui échouent dans un autre
    middleware, c'est-à-dire précisément celles qu'on cherche quand rien ne marche.
    """

    def __init__(self, application: Any, mesures: Mesures,
                 journal: logging.Logger | None = None) -> None:
        self.application = application
        self.mesures = mesures
        self.journal = journal or logging.getLogger("passerelle")

    async def __call__(self, portee: dict, recevoir: Any, envoyer: Any) -> None:
        if portee["type"] != "http":
            await self.application(portee, recevoir, envoyer)
            return

        entetes = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in portee.get("headers", [])}
        correlation = _correlation(entetes.get(ENTETE_CORRELATION, ""))
        debut = time.monotonic()
        statut = {"code": 500}

        async def envoyer_trace(message: dict) -> None:
            if message["type"] == "http.response.start":
                statut["code"] = message["status"]
                # L'identifiant repart avec la réponse : c'est ce qui permet à un
                # client de citer une requête précise au support.
                message.setdefault("headers", []).append(
                    (ENTETE_CORRELATION.encode(), correlation.encode()))
            await envoyer(message)

        try:
            await self.application(portee, recevoir, envoyer_trace)
        except Exception:
            duree = time.monotonic() - debut
            self._tracer(portee, correlation, 500, duree, exception=True)
            self.mesures.observer(_route(portee), portee.get("method", "?"), 500, duree)
            raise
        else:
            duree = time.monotonic() - debut
            self._tracer(portee, correlation, statut["code"], duree)
            self.mesures.observer(
                _route(portee), portee.get("method", "?"), statut["code"], duree)

    def _tracer(self, portee: dict, correlation: str, code: int, duree: float,
                exception: bool = False) -> None:
        # Le niveau suit la conséquence : une erreur serveur réveille quelqu'un, un
        # refus d'authentification non. Tout tracer au même niveau rend l'alerte
        # impossible à régler.
        niveau = (logging.ERROR if code >= 500
                  else logging.WARNING if code >= 400
                  else logging.INFO)
        self.journal.log(
            niveau, "requete",
            exc_info=exception,
            extra={
                "adsum_correlation": correlation,
                "adsum_methode": portee.get("method", "?"),
                # Le chemin déclaré, jamais le chemin appelé : celui-ci porte des
                # identifiants d'organisation et de commande.
                "adsum_route": _route(portee),
                "adsum_statut": code,
                "adsum_duree_ms": round(duree * 1000, 1),
            },
        )


def _route(portee: dict) -> str:
    """La route déclarée, avec ses paramètres nommés et non leurs valeurs.

    « /api/v1/portail/commandes/{reference} » et non la référence réelle : celle-ci
    identifie une commande, donc indirectement une organisation, et n'a rien à faire
    dans une mesure ni dans une trace.
    """
    route = portee.get("route")
    modele = getattr(route, "path", None)
    if modele:
        return str(modele)
    # Aucune route ne correspond : c'est un 404, ou un rejet avant le routage. Le
    # chemin brut serait alors une clé pilotée de l'extérieur, donc une cardinalité
    # sans limite dans les mesures et un chemin arbitraire dans les traces. Une
    # valeur unique suffit : ce qui compte est de savoir combien, pas lesquels.
    return "/inconnu"


def _correlation(fourni: str) -> str:
    """Reprendre l'identifiant de l'appelant, ou en créer un.

    Repris pour suivre une action à travers plusieurs services. Assaini avant d'être
    accepté : il repart dans un en-tête de réponse et dans les traces, donc une
    valeur libre venue de l'extérieur pourrait y injecter un saut de ligne et
    fabriquer une fausse ligne de journal.
    """
    propre = "".join(c for c in fourni if c.isalnum() or c in "-_")[:64]
    return propre or uuid.uuid4().hex
