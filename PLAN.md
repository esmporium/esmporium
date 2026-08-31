Rough plan for PRs.

Last updated: 2026-08-27 (ok, this is also tracked in git, anyway)

## PR1 Done, see https://github.com/esmporium/esmporium/pull/14

Add search

Check that we can execute searches using all our query classes.
Check that we can hit both the ESGF-1 and ESGF-NG APIs.
Check that we can track search API performance in a database.

Tests:

Unit tests:

- [x] conversion of query classes into ESGF-1 and ESGF-NG query parameters
    - no actual hit of the API, we just want to check the generated query parameters for each API
    - default is to always search so that retracted results are included too so we get tracking of when datasets go from not retracted to retracted (this means the retracted parameter will need to be passed every time I think, because ESGF's default is to not return retracted results)
- [x] check of retry logic i.e. make sure that retry wishes from the user are respected and passed through correctly
    - both retrying a given query with a given API and working our way through a list of APIs if the first one fails to give results (after retries)
- [x] *deferred to PR- [ ]5* check of search API performance database handling

Integration tests:

- [x] hit the APIs using all query classes and check that we get the results we expect. Run this on all combinations, even e.g. CMIP5 with ESGF-NG and CMIP7 with ESGF-1 We shouldn't get an error, we should just get no results.
    [x] check that all query - API combinations can be handled. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - only do 'plain' queries here, test the AND/OR logic of the ESGF APIs in a separate test below
        - the simplest will be e.g. CMIP5 with ESGF1
        - the most complicated will be a query using ESGFQuery that queries CMIP5, CMIP6 and CMIP7. This will require the queries to be split in parallel and retries of different end points for CMIP5 vs. CMIP6 vs. CMIP7. I guess the logic should be, if an end point gives no results, try the next one? For this PR, please just have some function like `end_point_selector` which is given the full query and the attempt number and returns the end-point to search. Give this sensible defaults but make sure that this function is injectable so a user can override the logic as they wish/need. This is what will also allow a user to effectively specify a queue of APIs to try as needed. For now, just combine the results in a basic dict with keys for each search e.g. `{"cmip5": cmip5_raw_json, "cmip6": cmip6_raw_json, "cmip7": cmip7_raw_json}` - we will clean this up in a follow up PR.
        - the form of this test will change in a follow up PR (and be easier to read and write) where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
    [x] check the AND/OR logic of the ESGF APIs. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - the simplest test should not have any test of AND/OR logic, because the search is so simple. However, the test should somehow be parameterised/duplicated so that there are also tests that check the ESGF APIs' AND/OR logic too.
        - this is testing ESGF behaviour, rather than ours, but it's still key to test in case ESGF makes a change
        - the form of this test will change in a follow up PR where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
        - do this only with our ESGFQuery class (don't worry about the specific ones, they're tested above), but this will need to be done on ESGF-1 ESGF-NG east and ESGF-NG west
    [x] deferred to PR1.5 check that the API health stats appear in the expected database
        - do this both for ESGF1 and ESGF-NG (don't worry about testing across all query classes though)
    [x] mark these tests so that they are opt-in. By default they should be skipped so that our 'main' test suite doesn't require slow ESGF queries to run
        - this pattern of keeping only 'fast' tests on the default testing branch is one we want to use throughout
        - we'll need different marks for different kinds of tests, that is ok
        - we'll probably want a 'run all' mark too for CI etc. Don't add it now, but just keep it in mind

Not included:

- converting results into Datasets
- preferencing API's based on past performance
    - we can add in such a helper later, if we want it. We're going to wait until we want it before we do this though
- tracking queries in our database, that comes later


## PR1.5

Search API health
If this PR is small enough, consider also adding a selector function that picks the API to use based on health information (e.g. pick the API which has the fastest response with results for the given project)
search.py logging see comment to return failure reason rather than None? (see line 137 in search.py)

Tests:

Unit tests:

- [x] capturing of various paths e.g. successful search (make sure we store request plus time taken for response plus whether there were any results/number of results (so we can distinguish APIs with results for projects vs. those without)?), failure search (store request plus failure status code plus failure status message plus ?), any other paths (not sure what...). Mock out the search API so we control the response

Integration tests:

- [x] check that the API health stats appear in the expected database
    - do this both for ESGF-1 ESGF15-bridge and ESGF-NG (don't worry about testing across all query classes though). This can just be part of the existing integration tests, no need to add new tests.
        - only done on select queries: no point duplicating the tests across code paths that won't vary by search API (we think)

## PR1.7

Use Search API health information from the database to select/rank APIs for the next request

This is an optional selector function to use if the SearchAPICallRecord table has data (any data, or only opt-in selector if the same request is being sent out? - maybe the latter?) This selector could take a few different formats: it could be based on no. results per request per host, or time per request per host, or results/time. Should this be a user decision, or we make that decision now? Our selector will rank the hosts based on database information, then return hosts based on that ranking (the search functions handle the logic around how many hosts are used, the selector functions don't need to worry about that logic at all).
ZN reply: it can only do something sensible if there is data. You have to decide what to do if the table has no data. I'd probably have a fallback option (e.g. the default selector) so that it doesn't blow up the first time it is used (and then kicks in once there is data)
ZN reply: I wouldn't do it only if the same request is being sent, but I can also see why that might be helpful. It doesn't really matter: we're just writing this as a convenience - users can always inject something else if they want so there is no real penalty to pay from getting this 'wrong'. Just use this PR as a chance to practice adding something and making decisions yourself with a lot of freedom. Maybe use a demo script that you can run repeatedly over different searches to help you figure out what is actually useful in practice.
ZN reply: it's all user decision. The point of the selector function being injectable is that the user can always choose to do something different.
ZN reply: all we want to demonstrate here is how such a selector could work, and to then put a couple that we think are most useful in the codebase so that a) others can get this without having to figure it out themselves and b) users have a template if they want to copy-paste then edit to get behaviour that is exactly tuned to their use case.

Where would a selector live? search_api.py?
ZN reply: ideally it should end up in search_api.py, next to the other in built selectors. However, given that it requires database knowledge, we might have to put it in `search_health` or somewhere else instead.

Update: Only rank by speed for this PR, leave project and more intelligent ranking (by results and speed according to project requests) for future PR.

Tests:

Unit tests:

- [ ] I feel a little lost with tests still, but I will have a crack and you can provide feedback.
    - Test that a mock health table with various hosts with different results and request times (and attempt numbers) rank the way we want them to. Creates output of ranked hosts that can be injected back to search.
      ZN reply: yep this is what we want

Integration tests:

- [ ] Are there any integration tests here? Feels like maybe just unit tests if we are only performing live tests on a single host?
    - ZN: yep no integration tests for this. The functionality is nice to have, not something we need to make bullet proof. Also setting up an integration test that was more than just running a bunch of searches then using this selector would be hard, and this simple integration test is very expensive to run and of very little value.

## PR2

Converting search results into Datasets

Check that we can parse search results into Datasets
Check whether we can create a mermaid diagram that, by definition, stays in line with our code that we can put in our docs to show the links between things
Include pagination in this step (currently we know the number of results found for each search, but are only recording the first 10,000 in the raw_json). STAC and SOLR will be handled differently. ~SOLR can be paralelised and STAC (with east and west having slightly different naming conventions), have to be handled consecutively.~ defer parallelisation to PR3.5
Defer parallelisation to PR3.5 ~This is the first place where we will be parallelising (handling multiple pages of results via SOLR). There are notes in tests/integration/test_health.py about defering parallelisation testing, primarily regarding how we handle recording results to a dataset in paralel (must be handled specifically).~

Tests:

Unit tests:

- [ ] parse a response from ESGF1, ESGF15-bridge and ESGF-NG into Dataset objects
    - use the pre-saved responses from PR1
    - don't save the Datasets into the database - that is an integration test
    - this will require adding the dataset version and dataset location objects (and I guess then also tables). The dataset location link may need to be nullable, because it doesn't exist for ESGF-NG?
    - I think this will also require adding in the project-specific dataset tables so we can parse CMIP5, CMIP6 and CMIP7 results properly
    - please also include a `dataset_addition_source` or similar column (I'm not sure what the best name is). In future, we will also want to add datasets e.g. from local files and I think we'll want to be able to tell whether a file was added because it was found in a query vs. because it was found in local files.
    - Defer to PR2.5 ~Pagination: we want a unit test for the pagination behaviour~

Integration tests:

- [ ] Update the integration tests from PR1 so that they now check the created Dataset objects, rather than the parsed raw JSON
- Defer to PR2.5 ~Pagination: an explicit integration test for a search that returns around 30 000 results (so we need 3 queries to get everything)~
- Defer to PR2.5 ~We can get the integration test with SOLR. With STAC, we'll have to rely on mocking for now to test this and a proper integration test can wait until there are 10 000 records available. -> or perhaps not...~
- Defer to PR3.5 ~SOLR paralelisation and saving multiple pages of results to dataset concurrently.~

Not included:

- parent stuff, that comes later

## PR2.5

Pagination

Unit tests:

- test pagination against a mock end point

Integration tests:

- Pagination: an explicit integration test for a search that returns around >10 queries when we set limit to 3 (so we need multiple queries to get everything)
    - by fiddling with limits, we should be able to set up an integration test for both SOLR and STAC

## PR3

Alter our search entrypoint

By this stage, we'll have two key functions (at least conceptually, maybe not in practice):

- [ ] `search`
- [ ] `parse_raw_json`
    - this will only parse into Datasets here. However, in future, this could also return DataAccess/File objects, so we need to be careful that the function signature and build of `parse_raw_json` accomodates for this (if not already, it should at least be clear how it would be modified to support DataAccess/File in future)

We want to add higher-level interfaces 'above' these.
For `search`, that means renaming the existing `search` to `search_single` (because it works with a single query) so we can then create our 'real' `search` function.
The new `search` function should handle splitting queries into project-specific queries, paralelise (although implementation is deferred until PR3.5), search ESGF, parse the raw JSON into datasets and save those datasets to the database.
We want it like this so that every search result goes in the database: that coupling is deliberate and, in many ways the point of esmporium. Users can use the low-level functions if they really want.

The new `search` function should take one or more queries.
In PR3.5, we will add handling of parallelisation of calls to `search_single`
(this is now simplified: we are going to set this up so that `search`
only ever passes queries that specify a single project to `search_single`
(we are never going to be fancy and put CMIP6 and CMIP6Plus searches together to save one query, because it makes implementation and error handling so much harder).

My instinct is to do it this way. Check this plan with claude first.

Unit tests:

- [ ] When a number of searches are done in parallel using `search`, if the process is killed in the middle of the parallel work, the results from the searches that did succeed are still in the database
    - this is the key check that the saving is done as part of `search_single`, rather than only happening after all search results have been collected in `search`
    - I have no idea how to implement this, ask claude (I think it should be possible to somehow kill a function halfway through, maybe just by raising an otherwise unhandled error...)
    - not a perfect unit test, but should be fast to run (by using a mock search endpoint) so can stay here

Integration tests:

- [ ] Update the integration tests from PR2 so that they use `search` or `search_single` (as is appropriate for the test)
- [ ] Add test of executing multiple queries in parallel using `search`
    - in the docstring of `search`, note that performing multiple queries with this function will be (once we do PR4) the only way to get certain types of logic e.g. if you want to look for `tas` monthly and `ts` daily but exclude `tas` daily and `ts` monthly, you need to do two queries because the ESGF API will give you `tas` and `ts` monthly and daily if you put in a search like `variable=["tas", "ts"], frequency=["mon", "day"]` (this is definitely true for SOLR, for NG it's probably not quite like that as the CQL2 syntax probably allows better AND/OR control - let's defer really deep support that right now, we can come back to it in future if we ever decide that we really need that feature/control)

Not included:

- there is no deliberately no attempt to cache in anyway here. If the user says 'search', we search (even if we already ran the same search 2 seconds previously) because the state of the ESGF database might have changed since we last looked (i.e. there is no sensible way to cache).

## PR3.5

Parallelisation of searching and saving

Unit tests:

- [ ]

Integration tests:

- [ ] parallelisation when we need pagination (i.e. have more than 10 000 results, although there are ways to test this that don't require getting more than 10 000 results e.g. set limit to 3 and get 13 results)
- [ ] parallelisation of pagination (might be overkill or overload servers (let's see what claude thinks), but might be helpful because we can calculate offset etc. without waiting for the previous query to come back)

## PR3.7

`QueryCollection`

Introduce a new object e.g. `QueryCollection` (name needs some work/thinking) that represents a collection of queries to ESGF.
As ESGF doesn't support all kinds of logic, this is what we actually need to support all different ways of creating searches
and hence what we should link to when we think about 'tracking changes in search results over time'.
Include an optional name parameter for this object.

Update `search` so it only takes `QueryCollection` objects.

Unit tests:

- [ ] auto-generation of name for `QueryCollection` objects. Include a timestamp so collision of names is very unlikely
- [ ] AND/OR logic with `QueryCollection` and search

Integration tess:

- [ ] update our `search` tests to use the new interface
- [ ] add `search` tests that use `QueryCollection`'s AND/OR logic (i.e. have multiple `Query`'s in the `QueryCollection`)

## PR4

Tracking changes in search results over time by tracking `QueryCollection`'s in the database

In the database, if a user tries to perform two searches which are the same `QueryCollection`, but they only differ by name,
the user should be able to choose what happens:
a) the name of the search is overwritten in the database
b) an error is raised
c) the search goes through but the old name is kept.
Make raising an error the default: we don't want people losing the ability to find the old name of their search by accident
and we don't want to support multiple names for a given search. Use an Enum (maybe best `StrEnum`) to represent the different options.

Check that we can track searches (i.e. `QueryCollection`'s) in our database
Check that we can repeat a search, given its `QueryCollection`'s ID or name (a user set thing) alone
Check that we can identify when, for a given search (i.e. `QueryCollection`),
metadata associated with a Dataset changes e.g. a Dataset goes from being not retracted to retracted

Tests:

Unit tests:

- [ ] handling of clashes in names (unit test because this logic test should be able to be done without a database connection if we get the abstraction right)
- [ ] passing of clash handling parameters from `search` down to low level functions (use mocking so this test is only checking parsing, not other behaviour)

(maybe we'll move some of the integration tests below in here if we can make them fast enough)

Integration tests:

- [ ] update existing tests to handle `search`'s new API
- [ ] searching with two query collections that only differ by name raises by default (parsing and other behaviour handled above)
- [ ] `search` tracks the search (i.e. `QueryCollection`) in the database
- [ ] we have a `rerun_search_esgf`, which works based on the query's name or ID alone (not sure if we need a specific function for this or we just make `search` handle query names, let's think about it). Re-running records the information required to see how dataset entries have changed over time
    - there's probably quite a few cases to consider here e.g. new dataset version, version goes from not retracted to retracted, dataset is no longer available (should never happen, because ESGF returns retracted and not retracted results by default, but it might look like data disappeared if a user set retracted=false in their search)
    - all of this tracking behaviour should also work if we just call `search` again with the same query (i.e. there should be some common path between the two, if we keep `rerun_search_esgf` and `search_esgf` separate)

Live tests:

- [ ] set up a new repo and, on the CMIP6 server, set up searches that get 'watched'. Once a day, re-run the search. If there are changes, email specific people
    - [ ] Malte search: CMIP7 daily and monthly for any experiment for the following variables: clt-evspsbl-hurs-huss-mrso-pr-psl-rlut-rsds-rsdt-rsut-rtmt-sfcWind-tas-tasmax-tasmin-ts-uas-vas
        - email you, me and Malte
    - [ ] Gregory search: (you know this one)
        - email you and me
    - [ ] Pattern scaling search: monthly tas for historical plus any of the `scen7-` experiments
        - email you, me and Spencer
    - [ ] Gang's search: (need to ask him)
        - email you, me and Gang
    - [ ] Pattern effect search: (you know this one)
        - email you and me

Not included:

-

## PR5

Helpers for making nice summaries of search results and changes in search results over time.

Let's put these helpers in esmporium, they're cheap and useful to others.
Let's drive their development based on what we see in the 'live tests' above.
I expect we'll just want to be able to convert search results to pandas' dataframes
because then we can just use tools like https://pypi.org/project/pandas-diff/
(although maybe there is a smarter, database native way to do this rather than going through pandas...).
Then we'll also have nice formatters, which initially we'll just put in our 'live test' repo
but we can move out into `esmporium` core when we're happy with them.

Tests:

Unit tests:

- [ ] Start from whatever intermediate changes object our low-level functions return (which I assume we'll need to have) and then run the formatter and check the formatted output
    - I would do this with pytest regressions by just saving the formatted output to a file, that is the easiest way to check output and changes over time in my experience (better than hard-coding strings to compare to in tests)
- [ ] parsing of formatting and other options through the higher-level API

Integration tests:

- [ ] Pre-load a database, then get the summaries
    - this is the kind of function we would use in our 'live tests' repository
    - this test is testing the actual function we care about: given I have a database, get me the summary
    - here we only test one path through (e.g. our default)
    - the unit tests then cover all the formatting options and behaviour and parameter parsing so we end up being confident that all the functionality works, without having to run lots of expensive integration tests

Not included:

-

## PR6

Finding dataset ancestry

Note that this is currently huge.
Before we start implementing, let's think about how to split this up, perhaps:

1. functionality to find a file that belongs to a dataset
1. functionality to retrieve the header of a file (also includes tracking of node health for this retrieval and the same 'selector' idea as we have for search)
1. addition of information only accessible from file headers to dataset entries in the database (link between that information and where it came from only comes in the next PR, when we start to save files in the database)
1. generation of a 'parent query' from a dataset in the database
1. (not needed, but noting): executing a parent query search and parsing results
1. linking parent and child datasets in the database
1. recursion i.e. adding ancestry back to a given level/experiment
1. caching/fast-path i.e. ensuring that we don't query again if we have information already or can get information from another dataset in the database (e.g. if we know parent for tas, we can use that to generate the parent query for rsdt, we don't need an rsdt file)

Also keep in mind that the above is very ESGF1 specific, the flow will likely be quite different for ESGF-NG so we'll need to think about that as we go too.

Will require adding the parent information to our dataset version table
Will require adding the file header query logic and parallelisation and node selection and health monitoring too
Will require adding some concept of fixes (initially do it in this package e.g. `src/esmporium/fixes`, can split out later) so we can inject corrected metadata on the fly

Tests:

Unit tests:

- [ ] we can navigate the ancestry in different cases
    - this is the key one because we should be able to cover lots of cases because the tests will be very fast as we will mock out all the calls to search and fetch file header etc.
- [ ] tests of file header retrieval that can use mocking
- [ ] tests of node health monitoring that can use mocking
- [ ] tests of node selection that can use mocking
- [ ] tests of node selection passing from high level down to low-level functions

Integration tests:

- [ ] start with a dataset, get its ancestry up to a given point, assert that all the expected parents and links end up in the database
    - [ ] key cases to cover
        - [ ] CMIP5 abrupt4xCO2
        - [ ] CMIP5 scenario
        - [ ] CMIP6 abrupt4xCO2 (pick a simulation with broken parent information here to test the fixes path)
        - [ ] CMIP6 scenario (or maybe even better, g6solar)
        - [ ] CMIP7 abrupt4xCO2
        - [ ] CMIP7 scenario
        - [ ] CMIP7 scenario extension when available
    - [ ] this will be very slow, because it will need to do lots of live API calls (ok, just make sure it is marked)
    - [ ] use parallelisation approaches and nodes that seem to be stable, so this test is as stable as possible
    - [ ] also assert that node health stats that we expect end up in the database
    - [ ] also assert that dataset information is updated as we expect
- [ ] 'fast path' testing i.e. pre-load a database with a dataset that already has all the parent information, then
    - [ ] test that if we ask for parent information to be added to this dataset again, no queries are actually done because all the information is already there
        - [ ] I don't think we need a `--force` flag that forces the queries to be re-run because the files attached to a dataset cannot be changed on ESGF without making a new version (and all of this parent stuff is version specific). This is one to check. A force flag isn't that expensive so maybe we just include it anyway, although it then means we have to track changes in parent information over time, which is tricky, so on second thoughts let's not include force for now, and we just note that this could be an issue in our docstring and wait and see if it is an issue before doing anything else.
    - [ ] test that if we ask for parent information of a dataset that should have the same parent information, no queries are done because we just use the parent information from the 'other' dataset instead
        - e.g. if we know parent information for tas, then we can get parent information for tos by just looking at the tas info, we don't need to re-query ESGF and read a file header
        - this tests the key optimisation that we need for large-scale parent attachment
        - we should note in our docstring clearly that this all relies on the assumption that we can do this 'cross-dataset' parent information jump. If we can't, the logic of this 'fast path' falls over (and then we'll need to introduce a `do_not_use_cross_dataset` flag or something, but again let's wait before doing that)

Not included:

- Addition of the File class/table. For now, just get the information you need from files and throw the full file information away. We will add File in the next PR

## PR7

Adding File i.e. data access tracking

Note that I think we should call this data access rather than file in preparation for the day when some data is made available by e.g. zarr archives, which aren't really 'files' in the way we normally think about it.
This probably means we'll need a DataAccess table with a type column, and only 'file' types have links to a `File` table (which then has further links to a `FileAccess` table). Other types would have different access patterns and information.

Use the old mermaid diagrams to help us think through the objects and tables we want (https://mermaid.live/edit#pako:eNqVV2- [ ]2zYQ_iuEvsTBbCOxncRxsAFBg67F2ixogg0YAgg0dbLZSKRKUmlcJ_99d5TkyBHtrf4kkvf63KvXkdAJRLMIzJXkC8Pze8XwdwvciOWXUrHn58FArxl-foZ8DsYuZcFmzIDQJrE7qK-44xbcuyVXC0BqofOidFCT169viP9C2VIrpL6Pltyyx-ps76MtroZsm_mTFtw13BbMIyQMTwqd-w8B72UGG50pHvbSP4esLbgB5eLa4PgBVo0IL7yl6FIIsNYzJfq7yjRP-BxJUqPzLk9Q2RJ4AoYVyKEdeuk0Mb6Nw7q6oJ9UjsmE3fzxemWdkWrBbAEi_mpR8H2ktMl5Ji1K_FaCWV2wRKYpQyglWNZyqcUPKik0io9Lk23rU2Uep7pUSffaOm0g6driuCvt63XCHTiZAxMG8DOJuevwOL5Ay3VBgecZEzzLEJmMzyFjPQUYD0zTb6VEfYeN-S_bKbjuCM25dWBijxhKr4M6YJfXVz6dBh-vEyjQdQx5ABOMy1cQXVulsk66kkxF2V33dWkEhF7gqcAY5JRggddHbiTHJ-90-BXzK8Tpdj0sjEx2yEsJTlBi1XlROpcYg9iA1Zn3MgR3k8frIDxceQQq3H9tBeKXphcE8E4qyVR07D0xDn5rtA03EgJ8tUQqREyv-vYCEcskXSQMi9qCL06nGff52GepNhgq45C2LXOudUYpbhE37HJuO-tJqfwBnUusBuynsU7jqu10UqnTVt56WOM5bKPXu3VQsMkFahHZsPHzMJSqlfytBCM8QGT4krCq0wy0ylYsB8cJatajVsXuPny8bUQfVG1zj4b9SVoT7a6AmoALJx-lW4VI5gbdX8bUMGKp4oqjQ1U5FJMHHnIPKY4LnTqGoFLjHcrkgmr4ERQBynT62mkbDMKtZDOB1rtyzavzyY1h3BvEHXkeU_8JNXKwizRuKsEH0S2h6lbU42UqBY2AXkvFM70eBpLYQJFJwcM6CGAUkRc09TA3vEGvtwG7Df_ejJgvl3-zDVNtLQ4YGlks0YL1HgAKy8h0eMJooym2zNzb1u3n47pTTU3jsKUxeoFleMEUjhSDg4Fw77WC0PcJq3geTNqfq7eAgEY4skqaEohmxrgwGid_ja5tswV7RC1LLEE82DLvtm_DxQN-hKqhznPunLEN-IQqGYbVusj0nCzCZzmnrYz1DkK1fhBCp11D3G8zgSqq1hysJXTblTiWV-g438JqM99rgfS-mfGtSNcb0_-IdwDSTZE3caxLfF99dUczbpNSUDQ_3N3d3NJuadgz-_MGrq8ub_Drd8SztAGZW2tRkxzWb134hAKrw0DjOlGtgbiE_WxJeh99zW_V-_5SxM_XUiQJrTrclFvUj2gXiGbOlNCPcsANkY6RD4ZPqRxR98ssNw_ERzwFV_9onTdsRpeLZTRLeWbxVBYU-PqvxoaEtinzDvdFF83OpudeRjRbR0_RbDA6Hw8no9Px6Gg8PR-NJuN-tMLr8dl0OB6fjKfjk-Oj0dHR5KUf_fBqj4fj6en5-PTkaHI8GZ-Npmf9CBKJa-fn6q-O_8fTj3jp9O1KicqKl38BECNB5A).

We want to avoid lots of project-specific fields if we can, but some might just make sense to keep (more tables, more problems, but maybe necessary).

Tests:

Unit tests:

- [ ] we can parse ESGF1 and ESGF-NG's raw JSON information about files into DataAccess, File, FileAccess etc. objects

Integration tests:

- [ ] Do a search query that gets file information from an ESGF API, assert that the expected data access information and links appear in the database
    - do this on all combinations of ESGF-1 ESGF-NG and CMIP5/CMIP6/CMIP7. Yes, that's more expensive, but it's also the key test so we are happy to pay that price
    - also make sure that there are links between information that can only be retrieved from file headers and the file from which the information was retrieved (add this assertion to the relevant tests from the previous PR)

Not included:

-

## PR8

Downloads

Tracking node health
Auto-preferencing nodes (but overridable by users)

Maybe too big and should be split, perhaps:

1. downloading a single file in isolation
1. tracking node health
1. parallelisation and (re-)queueing (will also require a basic selector function)
1. auto-selecting nodes based on health (fancy 'selector' function)

Tests:

Unit tests:

- [ ] parsing of parallelisation and node preferencing options from high- to low-level functions
- [ ] automated node preferencing logic
    - e.g. when a node is marked as dead, what controls whether one node is picked over another
    - we should be able to do this with a mock node health table/object so it is only a test of the preferencing logic, free from actual database calls
- [ ] preparation of download calls
    - my assumption is that we'll a flow something like: 1) specify the data of interest (maybe using a query object, maybe something else) 2) find all the datasets in the database that match 3) identify datasets that aren't available locally 4) put those datasets in a queue 5) do the downloading 6) update the database with the local download information
    - we can test the flow up by mocking out step 5, so we should be able to make this test very fast and cheap but still check all the key pieces of logic along the way
- [ ] handling of node failures and other responses within the download call
    - mock out the actual node responses with specific behaviour (e.g. a node that fails after a given amount of time, a node that returns a 404)
    - make sure that the behaviour is handled correctly
    - should be able to keep this as an integration test, as we can just adjust the failure tresholds and 'slow response' times together so the test only waits e.g. 1 second before declaring a node as a failure but we have confidence about this 'identifying failing node' and config (so have at least some confidence that a failing node can be handled)

Integration tests:

- [ ] actual downloading in parallel
    - [ ] populate a database with say 4 entries, then call download in a way that should only download two of them
    - [ ] pick files/datasets we know are small (e.g. areacella files or setting this up in a way that means we only need to download two years of EC Earth data, which is in one year files that are tiny)
    - [ ] actually download them
    - [ ] check that the data is there, the database state is as expected etc.

Live tests: download data for the live searches considered above (except for Malte's daily data, let's check with him before we start downloading anything as we might actually run out of space if we do that)

Not included:

- integration tests that hit a node we are confident is failing. I hope we can get the confidence we want from unit tests and can avoid having a live 'node failure wait' test. Let's make a note that we may want to revisit this decision in the test file

## PR9

Data loading

This needs to be thought through more carefully before we get started.

- overall loading flow
- ability to override intermediate steps
- linking with `esmporium-fixes` package
    - make this package in this repository initially (just make it in a path like `src/esmporium/fixes`)
    - we will split out into a separate repository later
- live test is setting up our gregory and other calculations on the CMIP6 server
    - this will be what really drives our testing strategy, as only once we try and do this will we see the convenience functions that we actually want
- the step of then passing our results out to other places e.g. sharing them via a web site is a separate concern. We'll go and speak to Jared about REF integration (or not) before making decisions there. However, I would like our archive of processed CMIP7 (and CMIP6 and CMIP5) output to start building up on the server while we think about this 'public facing' option as it will take a while to download and crunch all this data.
- probably implement using the same type annotation trick or something similar so the user has a way to specify to esmporium what groups it wants and how it wants its data loaded. Might need to express that in a class too/instead (that matches some interface/protcol). We should write up some pseudocode before we start implementing.

Tests:

Unit tests:

- [ ]

Integration tests:

- [ ]
