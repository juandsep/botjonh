from assistant.context import BUCKET_OF, CATEGORIES


def test_every_category_has_one_bucket() -> None:
    assert len(CATEGORIES) == len(set(CATEGORIES)) == len(BUCKET_OF) == 14
    assert BUCKET_OF["supermercado"] == "necesidades"
    assert BUCKET_OF["inversion"] == "ahorro"
