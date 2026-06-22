"""Annotation markers for DerivedTable source dataclasses."""


class Identity:
    """Annotated marker for identity fields. Determines row uniqueness."""


class ContentKey:
    """Annotated marker for content-hashing fields. Determines whether re-derivation is needed."""
