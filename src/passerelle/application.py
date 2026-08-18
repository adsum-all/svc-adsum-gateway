"""La passerelle, assemblée et prête à servir.

Un service à part, appelé par les autres services et jamais par un navigateur. Deux
conséquences directes sur ce fichier.

**Aucun partage d'origine n'est déclaré.** Pas de liste d'origines, pas d'étoile :
une page web n'a rien à faire ici. Ouvrir le partage d'origine sur un service qui
envoie des messages en le nom de l'éditeur reviendrait à laisser n'importe quelle
page en faire partir.

**L'appelant prouve son identité par un secret partagé, pas par un jeton
d'utilisateur.** Personne n'est derrière ces appels : c'est le service commerce qui
demande l'envoi d'une relance, ou l'ordonnanceur qui rejoue une file. Un jeton
d'utilisateur n'aurait aucun sens, et en exiger un obligerait à fabriquer un compte
de service, c'est-à-dire un compte réel avec un mot de passe réel qui traînerait.

L'exception est la réception des accusés de fournisseur : ceux-là arrivent d'Internet
et prouvent leur origine par leur signature, jamais par un secret d'appelant.
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse

from .journal import Journal
from .observabilite import Mesures, TraceRequetes, configurer
from .port import (
    Canal,
    CanalNonServi,
    Destinataire,
    DestinataireBloque,
    ErreurEnvoi,
    Message,
)
from .service import Registre, ServicePasserelle


@dataclass(frozen=True)
class Configuration:
    """Ce que la passerelle a besoin de savoir, lu de l'environnement.

    Aucune valeur de repli sur un secret : une passerelle qui démarre avec un secret
    d'appel vide accepte n'importe quelle demande d'envoi, et le défaut ne se voit
    qu'après que des messages sont partis en notre nom.
    """

    dsn: str
    #: Le secret que doivent présenter les services appelants. Sans lui, le service
    #: refuse de démarrer.
    secret_appel: str
    #: Les secrets nommés, un par appelant, au format « nom:secret » séparés par des
    #: virgules. Le registre retient alors qui a demandé chaque envoi, et un secret
    #: compromis se révoque sans couper les autres services.
    #:
    #: Le secret unique reste accepté et vaut « appelant inconnu » : couper les
    #: appelants existants le jour où l'on pose cette variable ferait taire les
    #: relances sans que rien ne l'annonce.
    secrets_nommes: str = ""
    brevo_cle: str = ""
    #: L'adresse d'expédition de l'éditeur, validée chez Brevo. C'est tout le sujet
    #: de ce service : ne plus faire partir les relances d'ADSUM par la chaîne
    #: d'envoi d'une organisation cliente.
    brevo_expediteur: str = ""
    brevo_nom: str = "ADSUM"
    brevo_secret_accuse: str = ""
    telegram_jeton: str = ""

    @classmethod
    def depuis_environnement(cls) -> "Configuration":
        manquants = [
            nom for nom in ("ADSUM_PASSERELLE_DSN", "ADSUM_PASSERELLE_SECRET")
            if not os.environ.get(nom)
        ]
        if manquants:
            raise RuntimeError(
                f"Variables requises absentes : {', '.join(manquants)}. La passerelle "
                "refuse de démarrer plutôt que d'accepter des demandes non vérifiées."
            )
        # Le même raisonnement que ci-dessus, poussé jusqu'au bout : un secret de
        # huit caractères se devine, et une passerelle qui démarre avec accepte des
        # demandes d'envoi sous l'identité de l'éditeur.
        if len(os.environ.get("ADSUM_PASSERELLE_SECRET", "")) < 32:
            raise RuntimeError(
                "ADSUM_PASSERELLE_SECRET fait moins de 32 caractères. Un secret "
                "court se devine, et il ouvre l'envoi de messages en notre nom.")
        lire = os.environ.get
        return cls(
            dsn=lire("ADSUM_PASSERELLE_DSN", ""),
            secret_appel=lire("ADSUM_PASSERELLE_SECRET", ""),
            secrets_nommes=lire("ADSUM_PASSERELLE_APPELANTS", ""),
            brevo_cle=lire("ADSUM_BREVO_CLE", ""),
            brevo_expediteur=lire("ADSUM_BREVO_EXPEDITEUR", ""),
            brevo_nom=lire("ADSUM_BREVO_NOM", "ADSUM"),
            brevo_secret_accuse=lire("ADSUM_BREVO_SECRET_ACCUSE", ""),
            telegram_jeton=lire("ADSUM_TELEGRAM_JETON", ""),
        )


def construire_registre(config: Configuration) -> Registre:
    """Les fournisseurs réellement configurés, et eux seuls.

    Un fournisseur enregistré sans ses identifiants ferait échouer tout message que
    le classement lui confierait, y compris ceux qu'un autre aurait pu acheminer.
    """
    registre = Registre()

    if config.brevo_cle and config.brevo_expediteur:
        from .adaptateurs.brevo import Brevo

        registre.enregistrer(Brevo(
            config.brevo_cle, config.brevo_expediteur, config.brevo_nom,
            config.brevo_secret_accuse,
        ), rang=10)

    if config.telegram_jeton:
        from .adaptateurs.telegram import Telegram

        registre.enregistrer(Telegram(config.telegram_jeton), rang=10)

    return registre


def creer_application(
    config: Configuration | None = None,
    ouvrir_connexion: Callable[[], Any] | None = None,
    registre: Registre | None = None,
) -> Any:
    """Monter l'application ASGI complète."""
    config = config or Configuration.depuis_environnement()

    def connexion_par_defaut() -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(config.dsn, row_factory=dict_row, connect_timeout=10)

    ouvrir = ouvrir_connexion or connexion_par_defaut
    registre = registre if registre is not None else construire_registre(config)

    mesures = Mesures()

    application = FastAPI(
        title="ADSUM Passerelle",
        description="La seule porte par laquelle ADSUM parle au monde.",
        version="0.1.0",
    )

    @contextmanager
    def service() -> Iterator[ServicePasserelle]:
        """Une connexion par requête, réellement fermée avec elle.

        Une connexion gardée au niveau du module survit à un basculement de base et
        rend des erreurs jusqu'au redémarrage. Mais la fermeture doit être écrite :
        elle ne se produisait qu'au ramassage des objets, c'est-à-dire jamais sur un
        chemin où une exception garde la trame vivante, ni sur un hébergeur qui gèle
        le processus après la réponse. Les connexions s'accumulaient alors jusqu'à
        épuiser la réserve du répartiteur, et la panne se manifestait ailleurs.
        """
        connexion = ouvrir()
        try:
            yield ServicePasserelle(Journal(connexion), registre)
        finally:
            connexion.close()

    appelants = _lire_appelants(config)

    def exiger_appelant(requete: Request) -> str:
        """Vérifier le secret et rendre le nom de l'appelant.

        La comparaison passe par compare_digest et non par l'égalité : une
        comparaison qui sort au premier octet différent laisse deviner le secret
        caractère par caractère en mesurant le temps de réponse.

        Tous les candidats sont comparés, sans court-circuit, pour la même raison :
        s'arrêter au premier qui correspond révélerait par la durée lequel a été
        accepté, donc combien d'appelants existent et dans quel ordre.
        """
        fourni = requete.headers.get("authorization", "")
        # « compare_digest » refuse deux chaînes dont l'une n'est pas ASCII et lève
        # TypeError, qui n'est pas un refus : elle sort en 500, tracée au niveau
        # erreur, et laisse donc un appelant non authentifié piloter le niveau
        # d'alerte du service avec un seul octet.
        if not fourni.isascii():
            raise Refus(401, "Appelant non authentifié.")

        reconnu = ""
        for nom, secret in appelants.items():
            if hmac.compare_digest(fourni, f"Bearer {secret}"):
                reconnu = nom
        if not reconnu:
            raise Refus(401, "Appelant non authentifié.")
        return reconnu

    configurer(os.environ.get("ADSUM_JOURNAL_NIVEAU", "INFO"))
    application.add_middleware(TraceRequetes, mesures=mesures)

    # -- Routes --------------------------------------------------------------

    @application.get("/health")
    def sante() -> dict[str, Any]:
        """Ouverte : une sonde qui exige un secret n'est pas une sonde.

        Ne révèle que ce qui est nécessaire pour savoir si le service est utile :
        les canaux servis, jamais les identifiants ni les adresses.
        """
        # Le strict nécessaire pour qu'un équilibreur décide de router ou non.
        # La liste des fournisseurs et les anomalies de configuration disent à qui
        # les lit par où passent nos messages et ce qui manque : elles sont derrière
        # le secret, sur la route des mesures.
        return {
            "service": "adsum-passerelle",
            "canaux": registre.canaux_servis(),
        }

    @application.get("/metrics")
    def metriques(requete: Request) -> Any:
        """Les mesures, au format lu par les outils de supervision.

        Fermée par le secret d'appel, contrairement à la sonde : le volume par canal
        et le taux d'échec disent à qui les lit quand la plateforme écrit, à qui, et
        si ses envois passent.
        """
        exiger_appelant(requete)
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            mesures.exposer(),
            media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.post("/api/v1/envois")
    def envoyer(requete: Request, charge: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Acheminer un message. Idempotent sur la clé fournie.

        Volontairement « def » et non « async def ». Tout le travail est bloquant :
        une connexion psycopg, puis un appel HTTP au fournisseur qui peut durer
        vingt secondes. Sur la boucle d'événements, cela gelait toutes les autres
        requêtes de l'instance pendant ce temps. Starlette exécute une route
        synchrone dans un fil de travail, ce qui rend l'attente inoffensive.
        """
        appelant = exiger_appelant(requete)

        canal = _canal(charge.get("canal"))
        adresse = str(charge.get("adresse") or "").strip()
        cle = str(charge.get("cle_idempotence") or "").strip()
        corps = str(charge.get("corps") or "")
        if not adresse or not cle or not corps:
            raise Refus(422, "Adresse, clé d'idempotence et corps sont obligatoires.")

        message = Message(
            canal=canal,
            destinataire=Destinataire(
                adresse=adresse,
                nom=charge.get("nom"),
                langue=str(charge.get("langue") or "fr")[:2],
            ),
            objet=str(charge.get("objet") or ""),
            corps=corps,
            corps_riche=charge.get("corps_riche"),
            cle_idempotence=cle,
            categorie=str(charge.get("categorie") or "transactionnel"),
            organisation=str(charge.get("organisation") or ""),
            metadonnees={str(k): str(v) for k, v in (charge.get("metadonnees") or {}).items()},
        )

        with service() as passerelle:
            envoi = passerelle.envoyer(message, appelant)
        return {
            "cle": envoi.cle_idempotence,
            "statut": envoi.statut.value,
            "fournisseur": envoi.fournisseur,
            "canal": envoi.canal.value,
            "reference": envoi.reference,
            "deja_fait": envoi.deja_fait,
            "envoye_le": envoi.envoye_le.isoformat() if envoi.envoye_le else None,
        }

    @application.get("/api/v1/envois/{cle}")
    def etat(cle: str, requete: Request) -> dict[str, Any]:
        exiger_appelant(requete)
        with service() as passerelle:
            trouve = passerelle.etat(cle)
        if trouve is None:
            raise Refus(404, "Aucun envoi sous cette clé.")
        return trouve

    @application.get("/api/v1/envois")
    def derniers(requete: Request) -> dict[str, Any]:
        """Le fil récent, pour l'exploitation. Jamais d'adresse en clair."""
        exiger_appelant(requete)
        parametres = requete.query_params
        with service() as passerelle:
            return {"envois": passerelle.derniers(
                parametres.get("organisation", ""),
                parametres.get("canal", ""),
                _entier(parametres.get("limite"), 50),
            )}

    @application.get("/api/v1/indicateurs")
    def indicateurs(requete: Request) -> dict[str, Any]:
        """De quoi voir qu'un canal se dégrade avant que le client ne le signale."""
        exiger_appelant(requete)
        with service() as passerelle:
            return passerelle.indicateurs(
                _entier(requete.query_params.get("heures"), 24))

    @application.delete("/api/v1/blocages/{canal}")
    def debloquer(canal: str, requete: Request,
                  charge: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Lever un blocage d'adresse. Réservé à un exploitant.

        Une adresse corrigée existe, et un blocage définitif sans levée possible
        condamnerait une organisation au silence pour une faute de frappe.
        """
        exiger_appelant(requete)
        adresse = str(charge.get("adresse") or "").strip()
        if not adresse:
            raise Refus(422, "Adresse manquante.")
        with service() as passerelle:
            return {"debloquee": passerelle.debloquer(_canal(canal).value, adresse)}

    #: Au-delà, le corps d'un accusé est refusé sans être lu. Aucun fournisseur
    #: n'envoie un accusé de cette taille : au-dessus, c'est une charge destinée à
    #: occuper le service, et l'analyser serait déjà lui donner raison.
    TAILLE_ACCUSE_MAX = 64 * 1024

    @application.post("/api/v1/taches/purge")
    def purger(requete: Request) -> dict[str, Any]:
        """Effacer ce qui a dépassé sa durée de conservation.

        Déclenchée par l'ordonnanceur, une fois par jour. Une durée de conservation
        qui n'existe que dans une politique écrite n'est pas une durée : c'est cette
        tâche qui la rend vraie.
        """
        exiger_appelant(requete)
        with service() as passerelle:
            return passerelle.purger()

    @application.post("/api/v1/accuses/{fournisseur}")
    def accuse(fournisseur: str, requete: Request,
               corps: bytes = Body(b"", media_type="application/json")
               ) -> dict[str, Any]:
        """Recevoir un retour de fournisseur.

        Sans secret d'appelant : celui-ci arrive d'Internet et prouve son origine par
        sa signature. Exiger en plus notre secret partagé obligerait à le confier à
        un tiers, c'est-à-dire à le donner.

        Volontairement « def », comme la route d'envoi et pour la même raison : tout
        le travail est bloquant, et sur la boucle d'événements il gèlerait les autres
        requêtes. Le défaut était pire ici que partout ailleurs, parce que c'est la
        seule route que n'importe qui peut appeler.

        L'ordre des opérations compte : la base n'est ouverte qu'après
        authentification. Auparavant, une requête forgée sans aucun secret ouvrait
        une connexion PostgreSQL, ce qui offrait à Internet une amplification vers
        la base.
        """
        if len(corps) > TAILLE_ACCUSE_MAX:
            raise Refus(413, "Accusé trop volumineux.")

        sans_base = ServicePasserelle(None, registre)  # type: ignore[arg-type]
        authentifie = sans_base.authentifier_accuse(
            fournisseur, dict(requete.headers), corps)
        with service() as passerelle:
            return passerelle.appliquer_accuse(fournisseur, authentifie)

    # -- Erreurs -------------------------------------------------------------

    @application.exception_handler(Refus)
    async def _refus(_: Request, erreur: Refus) -> JSONResponse:
        return JSONResponse({"detail": erreur.message}, status_code=erreur.code)

    @application.exception_handler(DestinataireBloque)
    async def _bloque(_: Request, erreur: DestinataireBloque) -> JSONResponse:
        # 409 et non 400 : la demande était valide, c'est l'état du destinataire qui
        # s'y oppose. L'appelant ne doit pas réessayer, et le code le lui dit.
        return JSONResponse({"detail": str(erreur), "reessayable": False},
                            status_code=409)

    @application.exception_handler(CanalNonServi)
    async def _canal_absent(_: Request, erreur: CanalNonServi) -> JSONResponse:
        return JSONResponse({"detail": str(erreur), "reessayable": False},
                            status_code=503)

    @application.exception_handler(ErreurEnvoi)
    async def _envoi(_: Request, erreur: ErreurEnvoi) -> JSONResponse:
        # 502 : la passerelle a fait son travail, c'est en aval que rien n'a pris.
        # L'appelant peut réessayer plus tard, et sa clé d'idempotence le protège.
        return JSONResponse({"detail": str(erreur), "reessayable": True},
                            status_code=502)

    return application


class Refus(Exception):
    """Une erreur destinée à l'appelant, avec son code HTTP."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _lire_appelants(config: Configuration) -> dict[str, str]:
    """Les secrets acceptés, par nom d'appelant.

    Le secret unique figure toujours, sous le nom « inconnu » : le retirer le jour
    où l'on pose les secrets nommés couperait les services déjà en place, et la
    panne se manifesterait par des relances qui ne partent plus, sans message.

    Un secret trop court est écarté avec un avertissement plutôt que d'être accepté
    en silence : la variable est éditée à la main, et une faute de frappe y produit
    sinon un appelant qui n'entre jamais, sans que rien ne dise pourquoi.
    """
    connus: dict[str, str] = {}
    for morceau in config.secrets_nommes.split(","):
        nom, _, secret = morceau.strip().partition(":")
        if not nom or not secret:
            continue
        if len(secret) < 32:
            logging.getLogger("passerelle").warning(
                "Secret de l'appelant « %s » trop court, ignoré.", nom)
            continue
        connus[nom] = secret
    if config.secret_appel:
        connus.setdefault("inconnu", config.secret_appel)
    return connus


def _canal(valeur: Any) -> Canal:
    try:
        return Canal(str(valeur or "").strip().lower())
    except ValueError:
        connus = ", ".join(c.value for c in Canal)
        raise Refus(422, f"Canal inconnu. Ceux qui existent : {connus}.") from None


def _entier(valeur: str | None, defaut: int) -> int:
    try:
        return int(valeur) if valeur else defaut
    except ValueError:
        return defaut
