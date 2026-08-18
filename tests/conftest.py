"""Ce que les deux suites de tests partagent.

Le montage de la base est ici plutôt que dupliqué : deux copies divergent, et la
seconde continue de passer sur un schéma que la première a cessé d'appliquer.
"""
from __future__ import annotations

import io
import json
import os
import urllib.parse
from pathlib import Path

import pytest

SCHEMA = "test_passerelle"
SECRET_BASE = Path("C:/Users/kouas/Documents/deepl-test/95-sr-adsum/.secret/supabase-secret-adsum.json")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _dsn() -> str | None:
    if not SECRET_BASE.exists():
        return None
    s = json.load(io.open(SECRET_BASE, encoding="utf-8"))["supabase"]
    if not s.get("db_password"):
        return None
    mdp = urllib.parse.quote(s["db_password"], safe="")
    return (f"postgresql://postgres.{s['project_id']}:{mdp}"
            f"@aws-0-{s['region']}.pooler.supabase.com:5432/postgres?sslmode=require")


@pytest.fixture(autouse=True)
def poivre_de_test(monkeypatch):
    """Le poivre du registre, pose avant chaque test.

    Sans cette barriere, la suite passait sur la machine ou la variable trainait
    dans le shell et tombait en conteneur, ce qui est exactement l'ordre dans
    lequel on ne veut pas decouvrir une dependance a l'environnement. Le service
    exige ce poivre a juste titre : il refuse de journaliser une adresse dont
    l'empreinte serait retrouvable par dictionnaire. Le test doit donc le fournir,
    pas s'en remettre a celui du poste.

    Les tests qui verifient precisement l'absence ou la faiblesse du poivre le
    reecrivent par monkeypatch ; ce montage ne fait que garantir un etat de depart
    defini, jamais herite.
    """
    from passerelle import journal

    monkeypatch.setenv("ADSUM_PASSERELLE_POIVRE", "poivre-de-test-" + "0" * 24)
    monkeypatch.setattr(journal, "_POIVRE", None)
    yield
    journal._POIVRE = None


@pytest.fixture(scope="module")
def base():
    dsn = os.environ.get("PASSERELLE_DSN") or _dsn()
    if not dsn:
        pytest.skip("Aucune base joignable")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        pytest.skip("psycopg absent")
    try:
        conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Base injoignable : {type(e).__name__}")

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        for fichier in sorted(MIGRATIONS.glob("*.sql")):
            cur.execute(io.open(fichier, encoding="utf-8").read())
    conn.commit()
    conn.close()

    def ouvrir():
        c = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
        with c.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
        c.commit()
        return c

    yield ouvrir

    fin = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)
    with fin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    fin.commit()
    fin.close()


class FournisseurObserve:
    """Note ce qu'on lui demande, répond ce qu'on lui a dit de répondre.

    Partagé par les deux suites : les vrais adaptateurs sont éprouvés contre leurs
    propres formats, celui-ci sert à provoquer une panne de fournisseur, ce
    qu'aucun service réel ne fait sur commande.
    """

    from passerelle.port import Canal as _Canal

    canaux = (_Canal.COURRIEL,)

    def __init__(self, code: str = "observe", panne: str | None = None,
                 bloque: bool = False) -> None:
        self.code = code
        self.libelle = code
        self.panne = panne
        self.bloque = bloque
        self.envois: list = []

    def envoyer(self, message):
        import datetime as _dt

        from passerelle.port import DestinataireBloque, Envoi, ErreurEnvoi, Statut

        self.envois.append(message)
        if self.bloque:
            raise DestinataireBloque("Adresse morte")
        if self.panne:
            raise ErreurEnvoi(self.panne)
        return Envoi(
            cle_idempotence=message.cle_idempotence,
            statut=Statut.ACCEPTE,
            fournisseur=self.code,
            canal=message.canal,
            reference=f"REF-{len(self.envois)}-{self.code}",
            envoye_le=_dt.datetime.now(_dt.timezone.utc),
        )
