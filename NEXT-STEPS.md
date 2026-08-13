## PR1

Add search

Check that we can execute searches using all our query classes.
Check that we can hit both the ESGF1 and ESGF-NG APIs.
Check that we can track search API performance in a database.

Tests:

Unit tests:

1. conversion of query classes into ESGF1 and ESGF-NG query parameters
    - no actual hit of the API, we just want to check the generated query parameters for each API
    - default is to always search so that retracted results are included too so we get tracking of when datasets go from not retracted to retracted (this means the retracted parameter will need to be passed every time I think, because ESGF's default is to not return retracted results)
1. check of retry logic i.e. make sure that retry wishes from the user are respected and passed through correctly
    - both retrying a given query with a given API and working our way through a list of APIs if the first one fails to give results (after retries)
1. check of search API performance database handling

Integration tests:

1. hit the APIs using all query classes and check that we get the results we expect. Run this on all combinations, even e.g. CMIP5 with ESGF-NG and CMIP7 with ESGF1. We shouldn't get an error, we should just get no results.
    1. check that all query - API combinations can be handled. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - only do 'plain' queries here, test the AND/OR logic of the ESGF APIs in a separate test below
        - the simplest will be e.g. CMIP5 with ESGF1
        - the most complicated will be a query using ESGFQuery that queries CMIP5, CMIP6 and CMIP7. This will require the queries to be split in parallel and retries of different end points for CMIP5 vs. CMIP6 vs. CMIP7. I guess the logic should be, if an end point gives no results, try the next one? For this PR, please just have some function like `end_point_selector` which is given the full query and the attempt number and returns the end-point to search. Give this sensible defaults but make sure that this function is injectable so a user can override the logic as they wish/need. This is what will also allow a user to effectively specify a queue of APIs to try as needed. For now, just combine the results in a basic dict with keys for each search e.g. `{"cmip5": cmip5_raw_json, "cmip6": cmip6_raw_json, "cmip7": cmip7_raw_json}` - we will clean this up in a follow up PR.
        - the form of this test will change in a follow up PR (and be easier to read and write) where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
    1. check the AND/OR logic of the ESGF APIs. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - the simplest test should not have any test of AND/OR logic, because the search is so simple. However, the test should somehow be parameterised/duplicated so that there are also tests that check the ESGF APIs' AND/OR logic too.
        - this is testing ESGF behaviour, rather than ours, but it's still key to test in case ESGF makes a change
        - the form of this test will change in a follow up PR where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
        - do this only with our ESGFQuery class (don't worry about the specific ones, they're tested above), but this will need to be done on ESGF1, ESGF-NG east and ESGF-NG west
    1. check that the API health stats appear in the expected database
        - do this both for ESGF1 and ESGF-NG (don't worry about testing across all query classes though)
    1. mark these tests so that they are opt-in. By default they should be skipped so that our 'main' test suite doesn't require slow ESGF queries to run
        - this pattern of keeping only 'fast' tests on the default testing branch is one we want to use throughout
        - we'll need different marks for different kinds of tests, that is ok
        - we'll probably want a 'run all' mark too for CI etc. Don't add it now, but just keep it in mind

Not included:

- converting results into Datasets
- preferencing API's based on past performance
    - we can add in such a helper later, if we want it. We're going to wait until we want it before we do this though
- tracking queries in our database, that comes later

## PR2

Converting search results into Datasets

Check that we can parse search results into Datasets
Check whether we can create a mermaid diagram that, by definition, stays in line with our code that we can put in our docs to show the links between things

Tests:

Unit tests:

1. parse a response from ESGF1 and ESGF-NG into Dataset objects
    - don't save the Datasets into the database - that is an integration test
    - this will require adding the dataset version and dataset location objects (and I guess then also tables). The dataset location link may need to be nullable, because it doesn't exist for ESGF-NG?
    - I think this will also require adding in the project-specific dataset tables so we can parse CMIP5, CMIP6 and CMIP7 results properly

Integration tests:

1. Update the integration tests from PR1 so that they now check the created Dataset objects, rather than having to parse raw JSON

Not included:

- parent stuff, that comes later

## PR3

Alter our search entrypoint

By this stage, we'll have two key functions (at least conceptually, maybe not in practice):

1. `search_esgf`
1. `parse_raw_json`
    - this will only parse into Datasets here. However, in future, this could also return DataAccess/File objects, so we need to be careful that the function signature and build of `parse_raw_json` accomodates for this (if not already, it should at least be clear how it would be modified to support DataAccess/File in future)

We want to make these both 'private' i.e. add an underscore at the start of them and then create our 'real' `search_esgf` function. Maybe call this `search_esgf_single`, because this function should only support a single query. This function should search ESGF, parse the raw JSON into datasets and save those datasets to the database. We want it like this so that every search result goes in the database: that coupling is deliberately and, in many ways the point of esmporium. Users can use the private functions if they really want, but we're marking them as 'private' to make clear that we think this pattern should only be used if you really know what you're doing.

Then, also add a higher-level `search_esgf` function, that takes one or more queries. This function handles the parallelisation of calls to `search_esgf_single` (but it is a bit more complicated, because `search_esgf_single` can itself also spin up multiple workers if e.g. it receives an ESGFQuery that needs to work over multiple projects).

My instinct is to do it this way. Check this plan with claude first. Maybe there is a better pattern for handling this parallelisation over queries (i.e. the fact that `search_esgf` needs to parallelise calls to `search_esgf_single`) and parallelisation over projects (i.e. the fact that `search_esgf_single` has to make multiple calls to ESGF if we want a query that includes multiple projects) issue.

Unit tests:

1. When a number of searches are done in parallel using `search_esgf`, if the process is killed in the middle of the parallel work, the results from the searches that did succeed are still in the database
    - this is the key check that the saving is done as part of `search_esgf_single`, rather than only happening after all search results have been collected in `search_esgf`
    - I have no idea how to implement this, ask claude (I think it should be possible to somehow kill a function halfway through, maybe just by raising an otherwise unhandled error...)
    - not a perfect unit test, but should be fast to run (by using a mock search endpoint) so can stay here

Integration tests:

1. Update the integration tests from PR2 so that they use `search_esgf` or `search_esgf_single` (as is appropriate for the test)
1. Add test of executing multiple queries in parallel using `search_esgf`
    - in the docstring of `search_esgf`, note that performing multiple queries with this function is the only way to get certain types of logic e.g. if you want to look for `tas` monthly and `ts` daily but exclude `tas` daily and `ts` monthly, you need to do two queries because the ESGF API will give you `tas` and `ts` monthly and daily if you put in a search like `variable=["tas", "ts"], frequency=["mon", "day"]`

Not included:

- there is no deliberately no attempt to cache in anyway here. If the user says 'search', we search (even if we already ran the same search 2 seconds previously) because the state of the ESGF database might have changed since we last looked (i.e. there is no sensible way to cache).

## PR4

Tracking changes in search results over time

Introduce a new object e.g. `QueryCollection` (name needs some work/thinking) that represents a collection of queries to ESGF. As ESGF doesn't support all kinds of logic, this is what we actually need to support all different ways of creating searches and hence what we should link to when we think about 'tracking changes in search results over time'. Include an optional name parameter for this object. In the database, if a user tries to perform two searches which are the same, but only differ by name, the user should be able to choose what happens: a) the name of the search is overwritten in the database b) an error is raised c) the search goes through but the old name is kept. Make raising an error the default: we don't want people losing the ability to find the old name of their search by accident and we don't want to support multiple names for a given search. Use an Enum (maybe best `StrEnum`) to represent the different options.
Check that we can track searches in our database
Check that we can repeat a search, given its ID or name (a user set thing) alone
Check that we can identify when metadata associated with a Dataset changes e.g. a Dataset goes from being not retracted to retracted

Update `search_esgf` so it only takes `QueryCollection` objects.

Tests:

Unit tests:

1. auto-generation of name for `QueryCollection` objects. Include a timestamp so collision of names is very unlikely
1. handling of clashes in names (unit test because this logic test should be able to be done without a database connection if we get the abstraction right)
1. passing of clash handling parameters from `search_esgf` down to low level functions (use mocking so this test is only checking parsing, not other behaviour)

(maybe we'll move some of the integration tests below in here if we can make them fast enough)

Integration tests:

1. update existing tests to handle `search_esgf`'s new API
1. running two searches that only differ by name raises by default (parsing and other behaviour handled above)
1. `search_esgf` tracks the search in the database
1. we have a `rerun_search_esgf`, which works baesd on the query's name or ID alone (not sure if we need a specific function for this or we just make `search_esgf` handle query names, let's think about it). Re-running records the information required to see how dataset entries have changed over time
    - there's probably quite a few cases to consider here e.g. new dataset version, version goes from not retracted to retracted, dataset is no longer available (should never happen, but might because I think ESGF only returns not retracted results by default, so it might look like data disappeared if a user set retracted=false in their search)
    - all of this tracking behaviour should also work if we just call `search_esgf` again with the same query (i.e. there should be some common path between the two, if we keep `rerun_search_esgf` and `search_esgf` separate)

Live tests:

1. set up a new repo and, on the CMIP6 server, set up searches that get 'watched'. Once a day, re-run the search. If there are changes, email specific people
    1. Malte search: CMIP7 daily and monthly for any experiment for the following variables: clt-evspsbl-hurs-huss-mrso-pr-psl-rlut-rsds-rsdt-rsut-rtmt-sfcWind-tas-tasmax-tasmin-ts-uas-vas
        - email you, me and Malte
    1. Gregory search: (you know this one)
        - email you and me
    1. Pattern scaling search: monthly tas for historical plus any of the `scen7-` experiments
        - email you, me and Spencer
    1. Gang's search: (need to ask him)
        - email you, me and Gang
    1. Pattern effect search: (you know this one)
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

1. Start from intermediate changes object (which I assume we'll need to have) and then run the formatter and check the formatted output
    - I would do this with pytest regressions by just saving the formatted output to a file, that is the easiest way to check output and changes over time in my experience (better than hard-coding strings to compare to in tests)
1. parsing of formatting and other options through the higher-level API

Integration tests:

1. Pre-load a database, then get the summaries
    - this is the kind of function we would use in our 'live tests' repository
    - this test is testing the actual function we care about: given I have a database, get me the summary
    - here we only test one path through (e.g. our default)
    - the unit tests then cover all the formatting options and behaviour and parameter parsing so we end up being confident that all the functionality works, without having to run lots of expensive integration tests

Not included:

-

## PR6

Finding dataset ancestry

Will require adding the parent information to our dataset version table
Will require adding the file header query logic and parallelisation and node selection and health monitoring too
Will require adding some concept of fixes (initially do it in this package e.g. `src/esmporium/fixes`, can split out later) so we can inject corrected metadata on the fly

Tests:

Unit tests:

1. we can navigate the ancestry in different cases
    - this is the key one because we should be able to cover lots of cases because the tests will be very fast as we will mock out all the calls to search and fetch file header etc.
1. tests of file header retrieval that can use mocking
1. tests of node health monitoring that can use mocking
1. tests of node selection that can use mocking
1. tests of node selection passing from high level down low-level functions

Integration tests:

1. start with a dataset, get its ancestry up to a given point, assert that all the expected parents and links end up in the database
    1. key cases to cover
        1. CMIP5 abrupt4xCO2
        1. CMIP5 scenario
        1. CMIP6 abrupt4xCO2 (pick a simulation with broken parent information here to test the fixes path)
        1. CMIP6 scenario (or maybe even better, g6solar)
        1. CMIP7 abrupt4xCO2
        1. CMIP7 scenario
        1. CMIP7 scenario extension when available
    1. this will be very slow, because it will need to do lots of live API calls (ok, just make sure it is marked)
    1. use parallelisation approaches and nodes that seem to be stable, so this test is as stable as possible
    1. also assert that node health stats that we expect end up in the database
    1. also assert that dataset information is updated as we expect
1. 'fast path' testing i.e. pre-load a database with a dataset that already has all the parent information, then
    1. test that if we ask for parent information to be added to this dataset again, no queries are actually done because all the information is already there
        1. I don't think we need a `--force` flag that forces the queries to be re-run because the files attached to a dataset cannot be changed on ESGF without making a new version (and all of this parent stuff is version specific). This is one to check. A force flag isn't that expensive so maybe we just include it anyway, although it then means we have to track changes in parent information over time, which is tricky, so on second thoughts let's not include force for now, and we just note that this could be an issue in our docstring and wait and see if it is an issue before doing anything else.
    1. test that if we ask for parent information of a dataset that should have the same parent information, no queries are done because we just use the parent information from the 'other' dataset instead
        - e.g. if we know parent information for tas, then we can get parent information for tos by just looking at the tas info, we don't need to re-query ESGF and read a file header
        - this tests the key optimisation that we need for large-scale parent attachment
        - we should note in our docstring clearly that this all relies on the assumption that we can do this 'cross-dataset' parent information jump. If we can't, the logic of this 'fast path' falls over (and then we'll need to introduce a `do_not_use_cross_dataset` flag or something, but again let's wait before doing that)

Not included:

- Addition of the File class/table. For now, just get the information you need from files and throw the full file information away. We will add File in the next PR

## PR7

Adding File i.e. data access tracking

Note that I think we should call this data access rather than file in preparation for the day when some data is made available by e.g. zarr archives, which aren't really 'files' in the way we normally think about it.
This probably means we'll need a DataAccess table with a type column, and only 'file' types have links to a `File` table (which then has further links to a `FileAccess` table). Other types would have different access patterns and information.

Use the old mermaid diagrams to help us think through the objects and tables we want (https://mermaid.live/edit#pako:eNqVV21v2zYQ_iuEvsTBbCOxncRxsAFBg67F2ixogg0YAgg0dbLZSKRKUmlcJ_99d5TkyBHtrf4kkvf63KvXkdAJRLMIzJXkC8Pze8XwdwvciOWXUrHn58FArxl-foZ8DsYuZcFmzIDQJrE7qK-44xbcuyVXC0BqofOidFCT169viP9C2VIrpL6Pltyyx-ps76MtroZsm_mTFtw13BbMIyQMTwqd-w8B72UGG50pHvbSP4esLbgB5eLa4PgBVo0IL7yl6FIIsNYzJfq7yjRP-BxJUqPzLk9Q2RJ4AoYVyKEdeuk0Mb6Nw7q6oJ9UjsmE3fzxemWdkWrBbAEi_mpR8H2ktMl5Ji1K_FaCWV2wRKYpQyglWNZyqcUPKik0io9Lk23rU2Uep7pUSffaOm0g6driuCvt63XCHTiZAxMG8DOJuevwOL5Ay3VBgecZEzzLEJmMzyFjPQUYD0zTb6VEfYeN-S_bKbjuCM25dWBijxhKr4M6YJfXVz6dBh-vEyjQdQx5ABOMy1cQXVulsk66kkxF2V33dWkEhF7gqcAY5JRggddHbiTHJ-90-BXzK8Tpdj0sjEx2yEsJTlBi1XlROpcYg9iA1Zn3MgR3k8frIDxceQQq3H9tBeKXphcE8E4qyVR07D0xDn5rtA03EgJ8tUQqREyv-vYCEcskXSQMi9qCL06nGff52GepNhgq45C2LXOudUYpbhE37HJuO-tJqfwBnUusBuynsU7jqu10UqnTVt56WOM5bKPXu3VQsMkFahHZsPHzMJSqlfytBCM8QGT4krCq0wy0ylYsB8cJatajVsXuPny8bUQfVG1zj4b9SVoT7a6AmoALJx-lW4VI5gbdX8bUMGKp4oqjQ1U5FJMHHnIPKY4LnTqGoFLjHcrkgmr4ERQBynT62mkbDMKtZDOB1rtyzavzyY1h3BvEHXkeU_8JNXKwizRuKsEH0S2h6lbU42UqBY2AXkvFM70eBpLYQJFJwcM6CGAUkRc09TA3vEGvtwG7Df_ejJgvl3-zDVNtLQ4YGlks0YL1HgAKy8h0eMJooym2zNzb1u3n47pTTU3jsKUxeoFleMEUjhSDg4Fw77WC0PcJq3geTNqfq7eAgEY4skqaEohmxrgwGid_ja5tswV7RC1LLEE82DLvtm_DxQN-hKqhznPunLEN-IQqGYbVusj0nCzCZzmnrYz1DkK1fhBCp11D3G8zgSqq1hysJXTblTiWV-g438JqM99rgfS-mfGtSNcb0_-IdwDSTZE3caxLfF99dUczbpNSUDQ_3N3d3NJuadgz-_MGrq8ub_Drd8SztAGZW2tRkxzWb134hAKrw0DjOlGtgbiE_WxJeh99zW_V-_5SxM_XUiQJrTrclFvUj2gXiGbOlNCPcsANkY6RD4ZPqRxR98ssNw_ERzwFV_9onTdsRpeLZTRLeWbxVBYU-PqvxoaEtinzDvdFF83OpudeRjRbR0_RbDA6Hw8no9Px6Gg8PR-NJuN-tMLr8dl0OB6fjKfjk-Oj0dHR5KUf_fBqj4fj6en5-PTkaHI8GZ-Npmf9CBKJa-fn6q-O_8fTj3jp9O1KicqKl38BECNB5A).

We want to avoid lots of project-specific fields if we can, but some might just make sense to keep (more tables, more problems, but maybe necessary).

Tests:

Unit tests:

1. we can parse ESGF1 and ESGF-NG's raw JSON information about files into DataAccess, File, FileAccess etc. objects

Integration tests:

1. Do a search query that gets file information from an ESGF API, assert that the expected data access information and links appear in the database
    - do this on all combinations of ESGF1/ESGF-NG and CMIP5/CMIP6/CMIP7. Yes, that's more expensive, but it's also the key test so we are happy to pay that price

Not included:

-

## PR8

Downloads

Tracking node health
Auto-preferencing nodes (but overridable by users)

Tests:

Unit tests:

1. parsing of parallelisation and node preferencing options from high- to low-level functions
1. automated node preferencing logic
    - e.g. when a node is marked as dead, what controls whether one node is picked over another
    - we should be able to do this with a mock node health table/object so it is only a test of the preferencing logic, free from actual database calls
1. preparation of download calls
    - my assumption is that we'll a flow something like: 1) specify the data of interest (maybe using a query object, maybe something else) 2) find all the datasets in the database that match 3) identify datasets that aren't available locally 4) put those datasets in a queue 5) do the downloading 6) update the database with the local download information
    - we can test the flow up by mocking out step 5, so we should be able to make this test very fast and cheap but still check all the key pieces of logic along the way
1. handling of node failures and other responses within the download call
    - mock out the actual node responses with specific behaviour (e.g. a node that fails after a given amount of time, a node that returns a 404)
    - make sure that the behaviour is handled correctly
    - should be able to keep this as an integration test, as we can just adjust the failure tresholds and 'slow response' times together so the test only waits e.g. 1 second before declaring a node as a failure but we have confidence about this 'identifying failing node' and config (so have at least some confidence that a failing node can be handled)

Integration tests:

1. actual downloading in parallel
    1. populate a database with say 4 entries, then call download in a way that should only download two of them
    1. pick files/datasets we know are small (e.g. areacella files or setting this up in a way that means we only need to download two years of EC Earth data, which is in one year files that are tiny)
    1. actually download them
    1. check that the data is there, the database state is as expected etc.

Live tests: download data for the live searches considered above (except for Malte's daily data, let's check with him before we start downloading anything as we might actually run out of space if we do that)

Not included:

- integration tests that hit a node we are confident is failing. I hope we can get the confidence we want from unit tests and can avoid having a live 'node failure wait' test. Let's make a note that we may want to revisit this decision in the test file

## PR9

Data loading

- overall loading flow
- ability to override intermediate steps
- linking with `esmporium-fixes` package
    - make this package in this repository initially (just make it in a path like `src/esmporium/fixes`)
    - we will split out into a separate repository later
- live test is setting up our gregory and other calculations on the CMIP6 server
    - this will be what really drives our testing strategy, as only once we try and do this will we see the convenience functions that we actually want
- the step of then passing our results out to other places e.g. sharing them via a web site is a separate concern. We'll go and speak to Jared about REF integration (or not) before making decisions there. However, I would like our archive of processed CMIP7 (and CMIP6 and CMIP5) output to start building up on the server while we think about this 'public facing' option as it will take a while to download and crunch all this data.

TODO: all the tests thinking (run out of steam for now)
