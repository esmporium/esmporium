"""
Tests of parsing what a file's global attributes say about its parent
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from esmporium.enrich.file_header import (
    UnknownProjectFileHeaderParserError,
    file_header_parser_for_project,
    parse_cmip5_file_header,
    parse_cmip6_file_header,
    parse_file_header,
)


class TestParsingWhatCmip6Publishes:
    """
    CMIP6 publishes the parent completely, so nothing is inferred
    """

    ATTRIBUTES: ClassVar[dict[str, object]] = {
        "parent_mip_era": "CMIP6",
        "parent_activity_id": "CMIP",
        "parent_source_id": "TaiESM1",
        "parent_experiment_id": "piControl",
        "parent_variant_label": "r1i1p1f1",
        "parent_time_units": "days since 0701-01-01",
        "branch_time_in_parent": 438000.0,
        "branch_time_in_child": 0.0,
    }

    def test_every_field_is_read(self):
        parent = parse_cmip6_file_header(self.ATTRIBUTES)

        assert parent.parent_project == "CMIP6"
        assert parent.parent_activity == "CMIP"
        assert parent.parent_model == "TaiESM1"
        assert parent.parent_experiment == "piControl"
        assert parent.parent_variant_label == "r1i1p1f1"
        assert parent.parent_time_units == "days since 0701-01-01"
        assert parent.branch_time_in_parent == 438000.0
        assert parent.branch_time_in_child == 0.0

    def test_a_root_is_declared_with_a_sentinel(self):
        """
        `parent_experiment_id = "no parent"` is a CMIP6 convention

        Which is a different convention from CMIP5's `"N/A"` and from CMIP7's, where a
        root simply carries none of the keys -- hence a sentinel list rather than one
        string.
        """
        parent = parse_cmip6_file_header({"parent_experiment_id": "no parent"})

        assert not parent.declares_a_parent

    def test_a_file_carrying_no_parent_keys_declares_no_parent(self):
        assert not parse_cmip6_file_header({"source_id": "TaiESM1"}).declares_a_parent

    def test_piControl_is_not_assumed_to_be_a_root(self):
        """
        A live CMIP6 `piControl` declares `piControl-spinup`, which nobody publishes

        Checked on 2026-11-05 against TaiESM1 and AWI-CM-1-1-MR. So the chain does not
        stop at `piControl`, and reading one as a root would be wrong -- which is why
        `find_parents` stops on a named experiment rather than on a guess.
        """
        parent = parse_cmip6_file_header(
            {
                "parent_experiment_id": "piControl-spinup",
                "parent_source_id": "TaiESM1",
                "parent_variant_label": "r1i1p1f1",
            }
        )

        assert parent.declares_a_parent
        assert parent.parent_experiment == "piControl-spinup"


class TestParsingWhatCmip5Publishes:
    """
    CMIP5 publishes three attributes and nothing else, so two have to be inferred
    """

    ATTRIBUTES: ClassVar[dict[str, object]] = {
        "parent_experiment_id": "piControl",
        "parent_experiment_rip": "r1i1p1",
        "branch_time": 240.0,
        # The file's own spelling of its model, which is *not* the spelling the search
        # index uses -- see `test_the_model_comes_from_the_database_not_the_file`.
        "model_id": "bcc-csm1-1-m",
        "project_id": "CMIP5",
        "institute_id": "BCC",
    }

    CHILD: ClassVar[dict[str, str]] = {
        "project": "CMIP5",
        # The Solr facet spelling, which is what `Dataset.model` holds.
        "model": "BCC-CSM1.1(m)",
    }

    def test_the_differently_named_fields_are_read(self):
        parent = parse_cmip5_file_header(self.ATTRIBUTES, **self.CHILD)

        assert parent.parent_experiment == "piControl"
        # CMIP5 calls the variant label `parent_experiment_rip`.
        assert parent.parent_variant_label == "r1i1p1"
        # And calls the branch time plain `branch_time`.
        assert parent.branch_time_in_parent == 240.0

    def test_the_model_comes_from_the_database_not_the_file(self):
        """
        The tempting mistake, and the one that would break every CMIP5 parent search

        CMIP5's search index spells several models differently from the files
        themselves. Checked live on 2026-11-05, the `model` facet says `INM-CM4`,
        `BCC-CSM1.1(m)`, `ACCESS1.0` and `CSIRO-Mk3.6.0` where the file's `model_id`
        -- and the DRS path -- say `inmcm4`, `bcc-csm1-1-m`, `ACCESS1-0` and
        `CSIRO-Mk3-6-0`.

        `Dataset.model` holds the facet spelling, because that is what a search is
        issued with and what the matcher compares. So a parent named with the file's
        spelling would be searched for and never found -- and the failure would look
        like "this parent is not published" rather than like a naming mismatch.
        """
        parent = parse_cmip5_file_header(self.ATTRIBUTES, **self.CHILD)

        assert parent.parent_model == "BCC-CSM1.1(m)"
        assert parent.parent_project == "CMIP5"
        # Emphatically not the file's own spelling.
        assert self.ATTRIBUTES["model_id"] == "bcc-csm1-1-m"
        assert parent.parent_model != self.ATTRIBUTES["model_id"]

    def test_what_cmip5_simply_does_not_have_is_left_alone(self):
        parent = parse_cmip5_file_header(self.ATTRIBUTES, **self.CHILD)

        # CMIP5 has no concept of an activity.
        assert parent.parent_activity is None
        # And publishes no units for its branch time, which is what makes that number
        # a record rather than a usable time.
        assert parent.parent_time_units is None
        assert parent.branch_time_in_child is None

    def test_a_root_run_has_nothing_inferred_onto_it(self):
        """
        The one thing that must not happen here

        A CMIP5 `piControl` says `parent_experiment_id = "N/A"` and nothing else --
        checked live on 2026-11-05 across bcc-csm1-1-m, bcc-csm1-1, BNU-ESM and
        CanESM2. Inferring a model and a project unconditionally would turn every root
        run in the archive into one asserting a parent it never named, which
        resolution could only fail on.
        """
        parent = parse_cmip5_file_header(
            {"parent_experiment_id": "N/A", "model_id": "bcc-csm1-1-m"},
            **self.CHILD,
        )

        assert not parent.declares_a_parent
        assert parent.parent_model is None
        assert parent.parent_project is None

    def test_one_unknown_field_does_not_make_the_record_a_root(self):
        """
        `"N/A"` means two different things in CMIP5, so it is judged per field

        `CMCC-CM`'s rcp85 names `historical` as its parent and `"N/A"` as the variant
        -- there one field is unknown and the record still declares a parent. The gap
        is left as a gap, for a fix to close.
        """
        parent = parse_cmip5_file_header(
            {"parent_experiment_id": "historical", "parent_experiment_rip": "N/A"},
            project="CMIP5",
            model="CMCC-CM",
        )

        assert parent.declares_a_parent
        assert parent.parent_experiment == "historical"
        assert parent.parent_variant_label is None
        # The inference still runs, because the record did declare a parent.
        assert parent.parent_model == "CMCC-CM"

    @pytest.mark.parametrize("branch_time", [0.0, 240.0, 185001.0, 20060101.0])
    def test_a_branch_time_is_recorded_verbatim_whatever_it_means(self, branch_time):
        """
        The same attribute carries at least five incompatible encodings

        Measured across 47 models: nothing recorded (`0.0`), a count of years
        (`240.0`), `YYYYMM` (`185001.0`), `YYYYMMDD` (`20060101.0`) and days since an
        unstated origin. With no `parent_time_units` published there is nothing to
        tell them apart, so this is stored as what a file said and never interpreted.
        """
        parent = parse_cmip5_file_header(
            {"parent_experiment_id": "piControl", "branch_time": branch_time},
            **self.CHILD,
        )

        assert parent.branch_time_in_parent == branch_time


class TestChoosingAParser:
    def test_each_project_gets_its_own(self):
        assert file_header_parser_for_project("CMIP5") is parse_cmip5_file_header
        assert file_header_parser_for_project("CMIP6") is parse_cmip6_file_header

    def test_cmip7_refuses_although_we_search_it(self):
        """
        A project we search is not therefore a project whose headers we read

        CMIP7 publishes its parent in the index, so nothing asks for its header, and
        no CMIP7 file has been read to find out what its attributes are called.
        Lending it CMIP6's parser would be the guess the test below rules out.
        """
        with pytest.raises(UnknownProjectFileHeaderParserError) as caught:
            file_header_parser_for_project("CMIP7")

        assert caught.value.project == "CMIP7"
        assert caught.value.known == ("CMIP5", "CMIP6")

    def test_an_unknown_project_refuses_rather_than_guessing(self):
        """
        Deliberately no default, unlike the parent *matcher*

        A parser that guessed which attributes carry the parent would not fail -- it
        would record that every dataset in the project is a root of the tree, which is
        a plausible-looking and completely wrong answer.
        """
        with pytest.raises(UnknownProjectFileHeaderParserError) as caught:
            file_header_parser_for_project("CORDEX")

        assert caught.value.project == "CORDEX"
        assert "CMIP5" in caught.value.known


def test_parse_file_header_hands_the_child_to_the_project_parser():
    """
    The child's project picks the parser, and its model is what CMIP5 infers
    """
    parent = parse_file_header(
        {"parent_experiment_id": "piControl", "parent_experiment_rip": "r1i1p1"},
        project="CMIP5",
        model="BCC-CSM1.1(m)",
    )

    assert parent.parent_model == "BCC-CSM1.1(m)"
    assert parent.parent_project == "CMIP5"
