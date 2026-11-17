"""Translated texts for the visible interface."""

from .config import LANGUAGES, load_texts


class Translator:
    """tr("menu.viewing") or tr("counter.working", n=3)."""

    def __init__(self, language: str):
        self.language = language
        self.texts = load_texts(language)

    def __call__(self, path: str, **values) -> str:
        node = self.texts
        for part in path.split("."):
            node = node[part]
        return node.format(**values) if values else node

    def group(self, path: str) -> dict:
        node = self.texts
        for part in path.split("."):
            node = node[part]
        return node


def all_translators() -> dict[str, Translator]:
    return {language: Translator(language) for language in LANGUAGES}
