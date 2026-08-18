"""Point d'entree de l hebergeur.

Le seul endroit ou la configuration est lue de l environnement : partout ailleurs
elle est passee en argument, ce qui rend le service montable dans un test sans
toucher aux variables du processus.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from passerelle.application import Configuration, creer_application  # noqa: E402

app = creer_application(Configuration.depuis_environnement())
