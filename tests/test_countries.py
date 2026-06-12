"""Tests for the canonical country dimension (common/countries.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from common.countries import (
    CANONICAL_COUNTRIES,
    ISO2_TO_ISO3,
    ISO3_TO_COUNTRY,
    normalize_countries,
    normalize_country,
)


class TestCanonicalPassthrough:
    """Canonical values must be returned unchanged (idempotence)."""

    def test_canonical_uppercase_unchanged(self):
        assert normalize_country("RUSSIA") == "RUSSIA"
        assert normalize_country("UNITED STATES") == "UNITED STATES"
        assert normalize_country("CONGO (DRC)") == "CONGO (DRC)"
        assert normalize_country("COTE D'IVOIRE") == "COTE D'IVOIRE"

    def test_idempotent_for_all_canonical(self):
        for name in CANONICAL_COUNTRIES:
            assert normalize_country(name) == name, name

    def test_case_insensitive_canonical(self):
        assert normalize_country("Russia") == "RUSSIA"
        assert normalize_country("russia") == "RUSSIA"
        assert normalize_country("United States") == "UNITED STATES"


class TestIsoCodes:
    def test_iso3(self):
        assert normalize_country("RUS") == "RUSSIA"
        assert normalize_country("usa") == "UNITED STATES"
        assert normalize_country("PRK") == "NORTH KOREA"
        assert normalize_country("CIV") == "COTE D'IVOIRE"

    def test_iso2(self):
        assert normalize_country("RU") == "RUSSIA"
        assert normalize_country("us") == "UNITED STATES"
        assert normalize_country("KP") == "NORTH KOREA"
        assert normalize_country("UA") == "UKRAINE"

    def test_iso2_table_consistency(self):
        """Every alpha-2 entry must point to a known alpha-3 entry."""
        for iso2, iso3 in ISO2_TO_ISO3.items():
            assert iso3 in ISO3_TO_COUNTRY, f"{iso2} -> {iso3} missing"

    def test_iso2_covers_all_iso3(self):
        """Every canonical country must be reachable via an alpha-2 code."""
        covered = set(ISO2_TO_ISO3.values())
        assert covered == set(ISO3_TO_COUNTRY.keys())


class TestAliases:
    """Alternate forms observed in real GEON sources."""

    def test_official_long_forms(self):
        # OpenCTI / STIX / sanctions style
        assert normalize_country("Russian Federation") == "RUSSIA"
        assert normalize_country("Syrian Arab Republic") == "SYRIA"
        assert normalize_country("Iran (Islamic Republic of)") == "IRAN"
        assert normalize_country("Venezuela, Bolivarian Republic of") == "VENEZUELA"
        assert normalize_country("Lao People's Democratic Republic") == "LAOS"
        assert normalize_country("Viet Nam") == "VIETNAM"
        assert normalize_country(
            "Democratic People's Republic of Korea") == "NORTH KOREA"
        assert normalize_country("Republic of Korea") == "SOUTH KOREA"

    def test_acled_style_names(self):
        assert normalize_country(
            "Democratic Republic of Congo") == "CONGO (DRC)"
        assert normalize_country("Ivory Coast") == "COTE D'IVOIRE"
        assert normalize_country("Myanmar") == "MYANMAR"

    def test_renamed_countries(self):
        assert normalize_country("Burma") == "MYANMAR"
        assert normalize_country("Czechia") == "CZECH REPUBLIC"
        assert normalize_country("Swaziland") == "ESWATINI"
        assert normalize_country("Turkiye") == "TURKEY"
        assert normalize_country("Türkiye") == "TURKEY"  # accented u

    def test_accents_and_punctuation(self):
        assert normalize_country("Côte d'Ivoire") == "COTE D'IVOIRE"
        assert normalize_country("Bosnia & Herzegovina") == "BOSNIA AND HERZEGOVINA"
        assert normalize_country("bosnia-herzegovina") == "BOSNIA AND HERZEGOVINA"

    def test_common_abbreviations(self):
        assert normalize_country("UK") == "UNITED KINGDOM"
        assert normalize_country("UAE") == "UNITED ARAB EMIRATES"
        assert normalize_country("DRC") == "CONGO (DRC)"

    def test_the_prefix(self):
        assert normalize_country("The Gambia") == "GAMBIA"
        assert normalize_country("the Netherlands") == "NETHERLANDS"


class TestAliasTableIntegrity:
    def test_all_alias_keys_are_pre_normalized(self):
        """Alias keys must survive _normalize_key unchanged, otherwise the
        entry is dead (lookups go through _normalize_key)."""
        from common.countries import ALIASES, _normalize_key
        bad = [k for k in ALIASES if _normalize_key(k) != k]
        assert not bad, f"unreachable alias keys: {bad}"

    def test_parenthesized_forms_resolve(self):
        assert normalize_country("North Korea (DPRK)") == "NORTH KOREA"
        assert normalize_country("Myanmar (Burma)") == "MYANMAR"
        assert normalize_country("Eswatini (Swaziland)") == "ESWATINI"

    def test_cameo_legacy_codes(self):
        assert normalize_country("KOS") == "KOSOVO"
        assert normalize_country("ROM") == "ROMANIA"
        assert normalize_country("MTN") == "MONTENEGRO"
        assert normalize_country("WSB") == "PALESTINE"
        assert normalize_country("GZS") == "PALESTINE"

    def test_seychelles(self):
        assert normalize_country("SC") == "SEYCHELLES"
        assert normalize_country("SYC") == "SEYCHELLES"
        assert normalize_country("Seychelles") == "SEYCHELLES"


class TestFallback:
    def test_unknown_uppercased(self):
        assert normalize_country("Atlantis") == "ATLANTIS"

    def test_actor_codes_passthrough(self):
        # GDELT actor type codes must keep passing through uppercased.
        assert normalize_country("GOV") == "GOV"
        assert normalize_country("MIL") == "MIL"

    def test_empty_and_none(self):
        assert normalize_country(None) == ""
        assert normalize_country("") == ""
        assert normalize_country("   ") == ""


class TestNormalizeCountries:
    def test_dedup_preserves_order(self):
        assert normalize_countries(
            ["Russia", "RUSSIA", "Ukraine", "RUS"]) == ["RUSSIA", "UKRAINE"]

    def test_drops_empties(self):
        assert normalize_countries(["", None, "France"]) == ["FRANCE"]

    def test_none_input(self):
        assert normalize_countries(None) == []


class TestProjectConsistency:
    """The other GEON country tables must agree with the canonical set."""

    def test_apt_mapping_keys_are_canonical(self):
        import json
        path = (Path(__file__).resolve().parent.parent
                / "ingestors" / "common" / "country_apt_mapping.json")
        keys = [k for k in json.load(path.open()) if k != "_comment"]
        for key in keys:
            assert normalize_country(key) == key, (
                f"country_apt_mapping.json key {key!r} is not canonical")

    def test_neighbors_keys_are_canonical(self):
        import json
        path = (Path(__file__).resolve().parent.parent
                / "ingestors" / "common" / "country_neighbors.json")
        data = json.load(path.open())
        for key, neighbors in data.items():
            if key == "_comment":
                continue
            assert normalize_country(key) == key, (
                f"country_neighbors.json key {key!r} is not canonical")
            for n in neighbors:
                assert normalize_country(n) == n, (
                    f"neighbor {n!r} of {key!r} is not canonical")

    def test_risk_score_targets_are_canonical(self):
        from risk_score.calculator import TARGET_COUNTRIES
        for country in TARGET_COUNTRIES:
            assert normalize_country(country) == country, (
                f"TARGET_COUNTRIES entry {country!r} is not canonical")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
