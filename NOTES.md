Notes

schema.py
- @claude: Dataset.grid_label, can be = Null here?
- esgf_doc_id clarify Solr vs STAC where this comes from

results_to_database.py
- COME BACK TO THIS SOME THINGS WEREN"T CLEAR
- @claude: processecor() remove search_host, why does the comment say it isn't used?

dataset_uniqueness.py
- @claude: "Empty if the datasets agree on every facet -- meaning nothing
        in the raw documents explains their `id_project_specific` difference."
    - This should fail loudly, not silently agree on all facets... something has gone wrong in workflow if get to this stage
- - It's a pure comparator, used widely. Two tests explicitly assert the empty return (test_facets_all_agree_on_yields_nothing, test_no_differences_when_every_facet_agrees), and the round-trip tests drive it. Making it raise changes its contract and breaks those.
- Empty isn't always a bug. For STAC (CMIP7, west CMIP6 without base_id), the distinguishing token can live only in the feature id, which _normalise_stac doesn't read (it flattens properties only). So two genuinely-different STAC datasets can flatten to identical facets → a legitimate empty, not a workflow error. (For Solr it's different: _normalise_solr keeps master_id/id, so differing ids always show up — empty there really would be anomalous.)
- The caller has the context to escalate well. The clash-resolution wrapper (the "which product?" flow — a future PR) is where "we found two datasets we can't tell apart and can't even explain why" should become a loud, user-facing error.
- in stac normalise don't only want to get properties back, want to investigate every difference.
