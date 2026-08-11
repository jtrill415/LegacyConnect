from strain_db import normalize
from strain_db.models import Environment, StrainType


class TestParseType:
    def test_plain(self):
        assert normalize.parse_type("Indica")[0] is StrainType.INDICA
        assert normalize.parse_type("Sativa")[0] is StrainType.SATIVA
        assert normalize.parse_type("Hybrid")[0] is StrainType.HYBRID

    def test_dominant(self):
        assert normalize.parse_type("Sativa-dominant Hybrid")[0] is StrainType.SATIVA_DOMINANT
        assert normalize.parse_type("Indica dominant")[0] is StrainType.INDICA_DOMINANT

    def test_ratio(self):
        t, ind, sat = normalize.parse_type("60% Indica / 40% Sativa")
        assert (ind, sat) == (60.0, 40.0)
        assert t is StrainType.INDICA_DOMINANT

    def test_ratio_sativa_first(self):
        t, ind, sat = normalize.parse_type("70% Sativa 30% Indica")
        assert (ind, sat) == (30.0, 70.0)
        assert t is StrainType.SATIVA_DOMINANT

    def test_single_pct_infers_complement(self):
        _, ind, sat = normalize.parse_type("80% indica")
        assert (ind, sat) == (80.0, 20.0)

    def test_balanced_ratio_is_hybrid(self):
        assert normalize.parse_type("50% Indica / 50% Sativa")[0] is StrainType.HYBRID

    def test_near_pure_promotes(self):
        assert normalize.parse_type("95% Indica / 5% Sativa")[0] is StrainType.INDICA

    def test_unknown(self):
        assert normalize.parse_type("")[0] is StrainType.UNKNOWN
        assert normalize.parse_type(None)[0] is StrainType.UNKNOWN
        assert normalize.parse_type("mystery")[0] is StrainType.UNKNOWN


class TestMeasurement:
    def test_range(self):
        m = normalize.parse_measurement("18-24%")
        assert (m.min, m.max, m.avg) == (18.0, 24.0, 21.0)

    def test_en_dash(self):
        m = normalize.parse_measurement("15–20 %")
        assert (m.min, m.max) == (15.0, 20.0)

    def test_single(self):
        assert normalize.parse_measurement("20.5%").avg == 20.5

    def test_up_to(self):
        m = normalize.parse_measurement("up to 27%")
        assert m.max == 27.0 and m.min is None

    def test_numeric(self):
        assert normalize.parse_measurement(19.2).avg == 19.2

    def test_comma_decimal(self):
        assert normalize.parse_measurement("18,5%").avg == 18.5

    def test_reversed_range_is_ordered(self):
        m = normalize.parse_measurement("24-18%")
        assert (m.min, m.max) == (18.0, 24.0)

    def test_empty(self):
        assert normalize.parse_measurement(None).is_empty()
        assert normalize.parse_measurement("n/a").is_empty()

    def test_merge_widens_range(self):
        a = normalize.parse_measurement("18-22%")
        b = normalize.parse_measurement("20-26%")
        merged = a.merged_with(b)
        assert (merged.min, merged.max) == (18.0, 26.0)


class TestCanonicalName:
    def test_basic(self):
        assert normalize.canonical_name("Blue Dream")[0] == "blue-dream"

    def test_case_and_space_insensitive(self):
        assert normalize.canonical_name("  BLUE   dream ")[0] == "blue-dream"

    def test_punctuation_stripped(self):
        assert normalize.canonical_name("O.G. Kush")[0] == normalize.canonical_name("OG Kush")[0]

    def test_alias_expansion(self):
        assert normalize.canonical_name("GSC")[0] == normalize.canonical_name("Girl Scout Cookies")[0]

    def test_parenthetical_ignored(self):
        assert (
            normalize.canonical_name("Wedding Cake (Triangle Mints #23)")[0]
            == normalize.canonical_name("Wedding Cake")[0]
        )

    def test_seed_noise_stripped(self):
        assert normalize.canonical_name("Blue Dream Feminized Seeds")[0] == "blue-dream"

    def test_autoflower_stays_distinct(self):
        auto, is_auto, _ = normalize.canonical_name("Auto Blue Dream")
        plain, _, _ = normalize.canonical_name("Blue Dream")
        assert is_auto is True
        assert auto != plain, "autoflower variants must not merge with the photoperiod strain"

    def test_feminized_flag(self):
        _, _, is_fem = normalize.canonical_name("Northern Lights Feminised")
        assert is_fem is True

    def test_hash_numbers_normalized(self):
        assert normalize.canonical_name("Gorilla Glue #4")[0] == "gorilla-glue-4"

    def test_accents(self):
        assert normalize.canonical_name("Crème Brûlée")[0] == "creme-brulee"

    def test_empty(self):
        assert normalize.canonical_name("")[0] == ""


class TestTerms:
    def test_exact(self):
        assert normalize.normalize_term("Relaxed", "effects") == "relaxed"

    def test_variant(self):
        assert normalize.normalize_term("Relaxing", "effects") == "relaxed"
        assert normalize.normalize_term("couch lock", "effects") == "sleepy"

    def test_negative_vocab(self):
        assert normalize.normalize_term("Cottonmouth", "negatives") == "dry_mouth"

    def test_flavor_variant(self):
        assert normalize.normalize_term("Lemon", "flavors") == "citrus"

    def test_terpene(self):
        assert normalize.normalize_term("Beta-Caryophyllene", "terpenes") == "caryophyllene"

    def test_unknown_returns_none(self):
        assert normalize.normalize_term("zzzz", "effects") is None

    def test_longest_variant_wins(self):
        # "dry eyes" must not be shadowed by a shorter partial match
        assert normalize.normalize_term("very dry eyes", "negatives") == "dry_eyes"

    def test_normalize_terms_dedupes_and_keeps_best_score(self):
        terms, unknown = normalize.normalize_terms(
            [("Relaxed", 0.8), ("Relaxing", 0.9), "zzz"], "effects"
        )
        assert len(terms) == 1
        assert terms[0].score == 0.9
        assert unknown == ["zzz"]

    def test_scores_rescaled_from_percent(self):
        terms, _ = normalize.normalize_terms([{"name": "happy", "score": 75}], "effects")
        assert terms[0].score == 0.75

    def test_score_none_preserved(self):
        terms, _ = normalize.normalize_terms(["happy"], "effects")
        assert terms[0].score is None


class TestGrow:
    def test_weeks(self):
        assert normalize.parse_flowering("8-10 weeks") == (56, 70)

    def test_single_week(self):
        assert normalize.parse_flowering("9 weeks") == (63, 63)

    def test_days(self):
        assert normalize.parse_flowering("56 - 63 days") == (56, 63)

    def test_none(self):
        assert normalize.parse_flowering(None) == (None, None)

    def test_environment(self):
        assert normalize.parse_environment("Indoor") is Environment.INDOOR
        assert normalize.parse_environment("Outdoor / greenhouse") is Environment.OUTDOOR
        assert normalize.parse_environment("Indoor & Outdoor") is Environment.UNKNOWN
        assert normalize.parse_environment(None) is Environment.UNKNOWN


class TestParents:
    def test_x_separator(self):
        assert normalize.split_parents("Blueberry x Haze") == ["Blueberry", "Haze"]

    def test_multiplication_sign(self):
        assert normalize.split_parents("OG Kush × Durban Poison") == ["OG Kush", "Durban Poison"]

    def test_slash(self):
        assert normalize.split_parents("Chemdawg / Hindu Kush") == ["Chemdawg", "Hindu Kush"]

    def test_drops_junk(self):
        assert normalize.split_parents("Unknown x ?") == []

    def test_empty(self):
        assert normalize.split_parents(None) == []

    def test_does_not_split_inside_word(self):
        # a bare "x" inside a word must not split the name
        assert normalize.split_parents("Maxi Gom") == ["Maxi Gom"]


class TestMisc:
    def test_rating_rescale(self):
        assert normalize.parse_rating("8.4", scale=10) == 4.2
        assert normalize.parse_rating("4.5", scale=5) == 4.5

    def test_rating_out_of_range(self):
        assert normalize.parse_rating("99", scale=5) is None

    def test_parse_int(self):
        assert normalize.parse_int("1,234 reviews") == 1234
        assert normalize.parse_int(None) is None

    def test_aliases(self):
        assert normalize.extract_aliases("Girl Scout Cookies (GSC)") == ["GSC"]
