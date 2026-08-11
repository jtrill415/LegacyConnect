from strain_db.merge import group_and_merge, merge_strains
from strain_db.models import (
    GrowInfo,
    Measurement,
    ScoredTerm,
    SourceRef,
    Strain,
    StrainType,
)


def make(source, name="Blue Dream", key="blue-dream", **kw):
    s = Strain(name=name, slug=key, canonical_key=key)
    s.sources = [SourceRef(source=source, url=f"https://{source}.test/{key}")]
    for field, value in kw.items():
        setattr(s, field, value)
    return s


class TestMergeScalars:
    def test_priority_wins_for_breeder(self):
        a = make("leafly", breeder="Leafly Co")
        b = make("seedfinder", breeder="DJ Short")
        merged = merge_strains([a, b])
        assert merged.breeder == "DJ Short", "seedfinder outranks leafly on breeder"
        assert merged.provenance["breeder"] == "seedfinder"

    def test_lower_priority_fills_gap(self):
        a = make("leafly", breeder=None)
        b = make("cannaconnection", breeder="Sensi Seeds")
        assert merge_strains([a, b]).breeder == "Sensi Seeds"

    def test_effects_prefer_leafly(self):
        a = make("seedfinder", effects=[ScoredTerm("happy", 0.2)])
        b = make("leafly", effects=[ScoredTerm("happy", 0.9)])
        merged = merge_strains([a, b])
        assert merged.effects[0].score == 0.9

    def test_unknown_type_does_not_override(self):
        a = make("leafly", type=StrainType.UNKNOWN)
        b = make("seedfinder", type=StrainType.INDICA)
        assert merge_strains([a, b]).type is StrainType.INDICA

    def test_single_record_passthrough(self):
        a = make("leafly")
        assert merge_strains([a]) is a


class TestMergeMeasurements:
    def test_range_widens_across_sources(self):
        a = make("leafly", thc=Measurement(min=17.0, max=22.0))
        b = make("weedmaps", thc=Measurement(min=19.0, max=26.0))
        merged = merge_strains([a, b])
        assert (merged.thc.min, merged.thc.max) == (17.0, 26.0)

    def test_empty_measurement_ignored(self):
        a = make("leafly", thc=Measurement())
        b = make("weedmaps", thc=Measurement(avg=20.0))
        assert merge_strains([a, b]).thc.avg == 20.0

    def test_provenance_records_all_contributors(self):
        a = make("leafly", thc=Measurement(min=17.0))
        b = make("weedmaps", thc=Measurement(max=26.0))
        assert "leafly" in merge_strains([a, b]).provenance["thc"]


class TestMergeLists:
    def test_union_of_effects(self):
        a = make("leafly", effects=[ScoredTerm("happy", 0.9)])
        b = make("weedmaps", effects=[ScoredTerm("sleepy", 0.5)])
        names = {t.name for t in merge_strains([a, b]).effects}
        assert names == {"happy", "sleepy"}

    def test_scored_terms_sort_first(self):
        a = make("leafly", effects=[ScoredTerm("happy", 0.9), ScoredTerm("euphoric", None)])
        merged = merge_strains([a, make("weedmaps")])
        assert merged.effects[0].name == "happy"

    def test_unscored_gets_score_from_other_source(self):
        a = make("seedfinder", effects=[ScoredTerm("happy", None)])
        b = make("leafly", effects=[ScoredTerm("happy", 0.7)])
        merged = merge_strains([a, b])
        assert len(merged.effects) == 1 and merged.effects[0].score == 0.7

    def test_parents_union(self):
        a = make("leafly", parents=["Blueberry"])
        b = make("seedfinder", parents=["Blueberry", "Haze"])
        assert merge_strains([a, b]).parents == ["Blueberry", "Haze"]

    def test_differing_names_become_aliases(self):
        a = make("leafly", name="Girl Scout Cookies")
        b = make("weedmaps", name="GSC")
        merged = merge_strains([a, b])
        assert "GSC" in merged.aka
        assert merged.name == "Girl Scout Cookies"


class TestMergeGrow:
    def test_seedfinder_wins_grow_data(self):
        a = make("leafly", grow=GrowInfo(flowering_days_min=50))
        b = make("seedfinder", grow=GrowInfo(flowering_days_min=63, yield_indoor="500g"))
        merged = merge_strains([a, b])
        assert merged.grow.flowering_days_min == 63
        assert merged.grow.yield_indoor == "500g"

    def test_gap_filled_from_lower_priority(self):
        a = make("seedfinder", grow=GrowInfo(flowering_days_min=63))
        b = make("leafly", grow=GrowInfo(yield_outdoor="700g"))
        assert merge_strains([a, b]).grow.yield_outdoor == "700g"


class TestGrouping:
    def test_exact_keys_group(self):
        out = group_and_merge([make("leafly"), make("weedmaps")])
        assert len(out) == 1
        assert len(out[0].sources) == 2

    def test_distinct_strains_stay_separate(self):
        out = group_and_merge(
            [make("leafly", name="Blue Dream", key="blue-dream"),
             make("leafly", name="Purple Haze", key="purple-haze")]
        )
        assert len(out) == 2

    def test_fuzzy_collapses_numeric_variant(self):
        out = group_and_merge(
            [make("leafly", name="Gorilla Glue 4", key="gorilla-glue-4"),
             make("seedfinder", name="Gorilla Glue", key="gorilla-glue")],
            fuzzy=True,
        )
        assert len(out) == 1

    def test_fuzzy_keeps_different_numbers_apart(self):
        """#1 and #2 of a cut are different strains and must not collapse."""
        out = group_and_merge(
            [make("leafly", name="Cookies 1", key="cookies-1"),
             make("leafly", name="Cookies 2", key="cookies-2")],
            fuzzy=True,
        )
        assert len(out) == 2

    def test_fuzzy_never_merges_auto_with_photoperiod(self):
        out = group_and_merge(
            [make("leafly", name="Blue Dream", key="blue-dream"),
             make("seedfinder", name="Auto Blue Dream", key="blue-dream-auto")],
            fuzzy=True,
        )
        assert len(out) == 2

    def test_fuzzy_off_keeps_near_misses_apart(self):
        out = group_and_merge(
            [make("leafly", key="gorilla-glue-4"), make("seedfinder", key="gorilla-glue")],
            fuzzy=False,
        )
        assert len(out) == 2

    def test_unrelated_names_never_merge(self):
        out = group_and_merge(
            [make("leafly", name="Blue Dream", key="blue-dream"),
             make("leafly", name="Blue Cheese", key="blue-cheese")],
            fuzzy=True,
        )
        assert len(out) == 2

    def test_empty_input(self):
        assert group_and_merge([]) == []
