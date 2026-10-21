"""Explicitly reviewed source/target contracts for offline OpenBao image upgrades."""

from copy import deepcopy

NAME = "openbao-2.7.1-patched-build"
SOURCE = {
    "image": "ghcr.io/openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf",
    "compose": "9c282baf31df205a554743e18abf79684e7d149434488784e5df099f11fc9dc3",
}
TARGET = {
    "image": "chipload-openbao:9303880cf60ff84dd47bc5350b48c1317a9c3e1019e8aea28e4f17671684bb8e",
    "compose": "3fdd0686a463f144ec20bb48c982667d040b7d5c8f262957d811d54e1411a707",
}


def transition(source, target, name=None):
    if name is None:
        if source != target:
            raise ValueError("recovery requires the backed-up OpenBao image and deployment version")
        return None
    if name != NAME or source != SOURCE or target != TARGET:
        raise ValueError("image upgrade requires the exact reviewed source and target contracts")
    return deepcopy({"name": NAME, "source": SOURCE, "target": TARGET})
