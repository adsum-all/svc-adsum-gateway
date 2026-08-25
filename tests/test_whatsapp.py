"""L'adaptateur WhatsApp, exercé contre les réponses réelles de l'API Meta.

Les charges de réponse reproduites ici viennent de la documentation Cloud API et des
codes que Meta renvoie effectivement. Elles servent à vérifier une traduction, jamais
à simuler un envoi : ce qui est testé, c'est ce que l'adaptateur construit et ce qu'il
conclut, les deux endroits où une erreur passe inaperçue jusqu'à la production.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from passerelle.adaptateurs.whatsapp import WhatsApp
from passerelle.application import Configuration, construire_registre
from passerelle.port import (
    Canal,
    Destinataire,
    DestinataireBloque,
    ErreurEnvoi,
    Message,
    Statut,
)
from passerelle.transport import Reponse

#: Valeur de signature propre aux tests, sans usage hors de ce fichier.
SIGNATURE_TEST = "-".join(("application", "meta", "de", "test", "seulement"))


class TransportProgramme:
    """Un transport qui rend une réponse décidée par le test et note l'appel.

    Ce n'est pas un double du fournisseur : c'est le fil, coupé à l'endroit exact où
    le réseau commence. Tout ce que l'adaptateur décide reste exercé pour de vrai.
    """

    def __init__(self, code: int = 200, charge: dict | None = None) -> None:
        self.code = code
        self.charge = charge if charge is not None else {
            "messages": [{"id": "wamid.HBgLMjI1MDcw"}]}
        self.appels: list[dict] = []

    def envoyer(self, methode, url, entetes, corps=None, delai=30.0):  # noqa: ANN001
        self.appels.append({
            "methode": methode, "url": url, "entetes": entetes,
            "corps": json.loads(corps.decode("utf-8")) if corps else None,
        })
        return Reponse(
            code=self.code, corps=json.dumps(self.charge).encode("utf-8"))


def _message(**remplace) -> Message:
    defauts = dict(
        canal=Canal.WHATSAPP,
        destinataire=Destinataire(adresse="+225 07 00 00 00 01", nom="Membre"),
        objet="Cotisation",
        corps="Votre cotisation arrive à échéance.",
        cle_idempotence="cle-1",
    )
    defauts.update(remplace)
    return Message(**defauts)


def _adaptateur(transport, **options) -> WhatsApp:
    return WhatsApp(
        "jeton-permanent-de-test", "123456789012345",
        transport=transport, **options)


# ------------------------------------------------------------------ construction


def test_refuse_de_se_construire_sans_jeton():
    with pytest.raises(ValueError, match="jeton"):
        WhatsApp("", "123456789012345")


def test_refuse_de_se_construire_sans_numero_expediteur():
    with pytest.raises(ValueError, match="identifiant du num"):
        WhatsApp("jeton", "")


# ------------------------------------------------------------------------ envoi


def test_envoi_en_texte_libre_construit_la_charge_attendue():
    transport = TransportProgramme()
    envoi = _adaptateur(transport).envoyer(_message())

    appel = transport.appels[0]
    assert appel["methode"] == "POST"
    assert appel["url"].endswith("/123456789012345/messages")
    assert appel["entetes"]["Authorization"].startswith("Bearer ")
    assert appel["corps"]["messaging_product"] == "whatsapp"
    assert appel["corps"]["type"] == "text"
    # Le sujet est recollé au corps : WhatsApp n'a pas de sujet, et le perdre laisse
    # un message dont on ne sait pas de quoi il parle.
    assert "Cotisation" in appel["corps"]["text"]["body"]
    assert "échéance" in appel["corps"]["text"]["body"]

    assert envoi.statut is Statut.ACCEPTE
    assert envoi.reference == "wamid.HBgLMjI1MDcw"
    assert envoi.canal is Canal.WHATSAPP


def test_le_numero_est_normalise_en_chiffres():
    transport = TransportProgramme()
    _adaptateur(transport).envoyer(_message())
    # 225 suivi des dix chiffres du numéro, sans le plus ni les espaces.
    assert transport.appels[0]["corps"]["to"] == "2250700000001"


def test_un_numero_sans_indicatif_est_refuse_avant_l_appel():
    transport = TransportProgramme()
    with pytest.raises(ErreurEnvoi, match="international"):
        _adaptateur(transport).envoyer(
            _message(destinataire=Destinataire(adresse="0700001")))
    assert transport.appels == []


def test_un_autre_canal_est_refuse():
    with pytest.raises(ErreurEnvoi):
        _adaptateur(TransportProgramme()).envoyer(_message(canal=Canal.SMS))


# --------------------------------------------------------------------- gabarits


def test_le_gabarit_nomme_par_l_appelant_est_utilise():
    transport = TransportProgramme()
    _adaptateur(transport).envoyer(_message(metadonnees={
        "gabarit": "rappel_cotisation",
        "gabarit_langue": "fr",
        "gabarit_parametre_1": "Membre",
        "gabarit_parametre_2": "31 octobre",
    }))
    charge = transport.appels[0]["corps"]
    assert charge["type"] == "template"
    assert charge["template"]["name"] == "rappel_cotisation"
    assert charge["template"]["language"]["code"] == "fr"
    valeurs = [p["text"] for p in charge["template"]["components"][0]["parameters"]]
    assert valeurs == ["Membre", "31 octobre"]


def test_la_lecture_des_parametres_s_arrete_au_premier_rang_absent():
    """Un trou dans la numérotation décalerait toutes les valeurs suivantes."""
    transport = TransportProgramme()
    _adaptateur(transport).envoyer(_message(metadonnees={
        "gabarit": "rappel", "gabarit_parametre_1": "un",
        "gabarit_parametre_3": "trois",
    }))
    valeurs = [
        p["text"]
        for p in transport.appels[0]["corps"]["template"]["components"][0]["parameters"]
    ]
    assert valeurs == ["un"]


def test_le_gabarit_par_defaut_sert_quand_l_appelant_n_en_nomme_aucun():
    transport = TransportProgramme()
    _adaptateur(transport, gabarit_defaut="avis_adsum").envoyer(_message())
    charge = transport.appels[0]["corps"]
    assert charge["template"]["name"] == "avis_adsum"
    # Sans rang déclaré, le corps devient la valeur unique du gabarit.
    assert len(charge["template"]["components"][0]["parameters"]) == 1


def test_la_langue_du_destinataire_prime_sur_celle_par_defaut():
    transport = TransportProgramme()
    _adaptateur(transport, gabarit_defaut="avis", langue_defaut="fr").envoyer(
        _message(destinataire=Destinataire(adresse="22507000001", langue="en")))
    assert transport.appels[0]["corps"]["template"]["language"]["code"] == "en"


def test_un_parametre_est_aplati_car_meta_refuse_les_sauts_de_ligne():
    """Meta rejette le message entier, pas le seul paramètre fautif."""
    transport = TransportProgramme()
    _adaptateur(transport).envoyer(_message(
        objet="", corps="Première ligne\nDeuxième ligne\t suite    espacée",
        metadonnees={"gabarit": "avis"}))
    valeur = (
        transport.appels[0]["corps"]["template"]["components"][0]["parameters"][0]
    )["text"]
    assert "\n" not in valeur and "\t" not in valeur
    assert "    " not in valeur


def test_un_parametre_trop_long_est_borne_a_la_limite_de_meta():
    transport = TransportProgramme()
    _adaptateur(transport).envoyer(_message(
        objet="", corps="a" * 2000, metadonnees={"gabarit": "avis"}))
    valeur = (
        transport.appels[0]["corps"]["template"]["components"][0]["parameters"][0]
    )["text"]
    assert len(valeur) == 1024


# ------------------------------------------------------------------------ refus


def _refus(code: int, message: str = "refus") -> TransportProgramme:
    return TransportProgramme(
        code=400, charge={"error": {"code": code, "message": message}})


def test_hors_fenetre_le_refus_dit_qu_il_faut_un_gabarit():
    """Le code 131047 se répare, il ne condamne pas le destinataire."""
    with pytest.raises(ErreurEnvoi, match="gabarit approuv") as capture:
        _adaptateur(_refus(131047)).envoyer(_message())
    assert not isinstance(capture.value, DestinataireBloque)


def test_un_numero_sans_compte_whatsapp_bloque_le_destinataire():
    with pytest.raises(DestinataireBloque):
        _adaptateur(_refus(131026)).envoyer(_message())


def test_un_numero_non_enregistre_bloque_le_destinataire():
    with pytest.raises(DestinataireBloque):
        _adaptateur(_refus(133010)).envoyer(_message())


def test_un_jeton_perime_est_un_echec_d_envoi_pas_un_blocage():
    """Le repli sur un autre fournisseur doit rester possible."""
    with pytest.raises(ErreurEnvoi, match="jeton") as capture:
        _adaptateur(_refus(190)).envoyer(_message())
    assert not isinstance(capture.value, DestinataireBloque)


def test_un_gabarit_inconnu_est_signale_comme_tel():
    with pytest.raises(ErreurEnvoi, match="gabarit"):
        _adaptateur(_refus(132001)).envoyer(_message(
            metadonnees={"gabarit": "inexistant"}))


def test_une_limitation_de_debit_invite_a_reessayer():
    with pytest.raises(ErreurEnvoi, match="plus tard"):
        _adaptateur(_refus(130429)).envoyer(_message())


def test_un_refus_inconnu_garde_le_motif_du_fournisseur():
    with pytest.raises(ErreurEnvoi, match="motif du fournisseur"):
        _adaptateur(_refus(999999, "motif du fournisseur")).envoyer(_message())


# ---------------------------------------------------------------------- accusés


def _signer(corps: bytes, secret: str = SIGNATURE_TEST) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), corps, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={signature}"}


def _retour(statut: str, erreurs: list | None = None) -> bytes:
    evenement = {
        "id": "wamid.HBgLMjI1MDcw", "status": statut,
        "recipient_id": "22507000001", "timestamp": "1755000000",
    }
    if erreurs:
        evenement["errors"] = erreurs
    return json.dumps({"entry": [{"changes": [
        {"value": {"statuses": [evenement]}}]}]}).encode("utf-8")


def test_sans_secret_configure_l_accuse_est_refuse():
    corps = _retour("delivered")
    with pytest.raises(ErreurEnvoi, match="secret"):
        _adaptateur(TransportProgramme()).verifier_accuse(_signer(corps), corps)


def test_une_signature_invalide_est_refusee():
    corps = _retour("delivered")
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    with pytest.raises(ErreurEnvoi, match="[Ss]ignature"):
        adaptateur.verifier_accuse(_signer(corps, "un-autre-secret"), corps)


def test_une_signature_absente_est_refusee():
    corps = _retour("delivered")
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    with pytest.raises(ErreurEnvoi, match="[Ss]ignature"):
        adaptateur.verifier_accuse({}, corps)


def test_un_accuse_signe_de_remise_est_traduit():
    corps = _retour("delivered")
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    accuse = adaptateur.verifier_accuse(_signer(corps), corps)
    assert accuse.statut is Statut.REMIS
    assert accuse.reference == "wamid.HBgLMjI1MDcw"
    assert accuse.adresse == "22507000001"
    assert accuse.recu_le is not None


def test_un_envoi_accepte_n_est_pas_une_remise():
    corps = _retour("sent")
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    assert adaptateur.verifier_accuse(_signer(corps), corps).statut is Statut.ACCEPTE


def test_un_echec_definitif_bloque_le_destinataire():
    corps = _retour("failed", [{"code": 131026, "title": "Message undeliverable"}])
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    accuse = adaptateur.verifier_accuse(_signer(corps), corps)
    assert accuse.statut is Statut.BLOQUE
    assert accuse.motif == "Message undeliverable"


def test_un_echec_reparable_est_un_refus_pas_un_blocage():
    corps = _retour("failed", [{"code": 131047, "title": "Re-engagement message"}])
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    assert adaptateur.verifier_accuse(_signer(corps), corps).statut is Statut.REFUSE


def test_un_message_entrant_n_est_pas_un_accuse():
    corps = json.dumps({"entry": [{"changes": [
        {"value": {"messages": [{"from": "22507000001", "text": {"body": "bonjour"}}]}}
    ]}]}).encode("utf-8")
    adaptateur = _adaptateur(TransportProgramme(), secret_application=SIGNATURE_TEST)
    with pytest.raises(ErreurEnvoi, match="aucun changement"):
        adaptateur.verifier_accuse(_signer(corps), corps)


# ----------------------------------------------------------- sonde et registre


def test_la_sonde_interroge_le_numero_expediteur():
    transport = TransportProgramme(charge={"verified_name": "ADSUM"})
    assert _adaptateur(transport).sante() is True
    assert transport.appels[0]["methode"] == "GET"


def test_la_sonde_signale_un_compte_injoignable():
    assert _adaptateur(TransportProgramme(code=401, charge={})).sante() is False


def _config(**remplace) -> Configuration:
    defauts = dict(dsn="postgres://exemple", secret_appel="s" * 32)
    defauts.update(remplace)
    return Configuration(**defauts)


def test_le_registre_sert_whatsapp_quand_les_deux_identifiants_sont_poses():
    registre = construire_registre(_config(
        whatsapp_jeton="jeton", whatsapp_numero="123456789012345"))
    assert [f.code for f in registre.pour(Canal.WHATSAPP)] == ["whatsapp"]


def test_un_jeton_sans_numero_n_enregistre_rien():
    """Un fournisseur incomplet ferait échouer ce qu'un autre aurait pu acheminer."""
    registre = construire_registre(_config(whatsapp_jeton="jeton"))
    assert registre.pour(Canal.WHATSAPP) == []


def test_le_canal_whatsapp_sans_fournisseur_est_signale_au_demarrage():
    anomalies = construire_registre(_config()).controle_de_coherence()
    assert any("whatsapp" in ligne for ligne in anomalies)
