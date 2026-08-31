Notes that can help us shape future docs.

## Search docs

When we come to writing search docs,
that is where we should go through the fact that users can write queries
in the query class they are used to and we handle the translation.

E.g. The key idea here is that we support specifying queries in multiple different ways.
We have our own way, implemented by [Query],
but users can also write queries in the form they are most familiar with,
e.g. [QueryCMIP5], which uses `ensemble` rather than `variant_label`,
[QueryCMIP6], which uses `source_id` rather than `model`
or [QueryCMIP7], which uses `branding_suffix` rather than `processing_id`.

The details of how this works don't really matter,
but we should cover the details of when this works and when it doesn't.

Also note that throughout search,
the facets are what we handle passing and translating
(except for `other_facets`),
but the values are ultimately yours to handle and check.
We do have some helpers to help with identifying potential typos,
but we do not control the ESGF index
so we would be lying if we pretended to be able to validate values
(note this might be a bit different for CMIP7 thanks to esgvoc and QA/QC,
but let's not assume that actually works right now).

## Search and query and vocab explanation

There are two relevant things for searching ESGF.
The first is the endpoint/host you're going to hit, e.g. "esgf.nci.org.au".
The second is the facets you're going to use to parse your search values.
So, while it is only one endpoint, it effectively acts as multiple endpoints
because the results you get back depend on the facets (i.e. argument names) you use.

If the ESGF search API were python functions, it is like it has been defined like this

```python
def search_esgf_like(
    ...,
    variable: str | None = None,
    variable_id: str | None = None,
    experiment: str | None = None,
    experiment_id: str | None = None,
    ...
) -> Results:
    # Some logic in here to decide what to do if e.g. variable and variable_id are passed.
    # From what I can tell, that logic creates surprising and confusing results.
    ...
```

That design is obviously really difficult to maintain and use.
You need to cover all the edge cases:
what happens if a user supplies both `variable` and `variable_id`,
what if they supply neither,
what if they supply `variable_id` but we're looking up CMIP5 data
so everything is saved under `variable`.
It's very clear that these edge cases haven't been covered,
which is why the ESGF search endpoints require such care to work with.

It would be much easier if they were defined with an endpoint specific to each project,
something like

```python
def search_cmip5(
    ...,
    variable: str | None = None,
    # Does not accept variable_id, parsing variable_id would be an instant error
    experiment: str | None = None,
    # Does not accept experiment_id, parsing experiment_id would be an instant error
    ...
) -> Results:
    # No logic required to parse e.g. both variable and variable_id being passed.
    ...

# Separate endpoint if you are searching a different project with different facet names
def search_cmip6(
    ...,
    variable_id: str | None = None,
    # Does not accept variable, parsing variable would be an instant error
    experiment_id: str | None = None,
    # Does not accept experiment, parsing experiment would be an instant error
    ...
) -> Results:
    # No logic required to parse e.g. both variable and variable_id being passed.
    ...

# etc. for other projects
```

In esmporium, we effectively add the `search_cmip5` and `search_cmip6` functions ourselves
via our search API facades.
The point of this is that the user gets an experience with much more robust query generation, result parsing and error handling.
It just means we have to deal with the headache of making sure that the translation to the raw search API classes is correct,
and we are also trying to make it possible for the user to effectively still call the raw search APIs directly,
in case we get this translation wrong (this is what our search API classes and `other_facets` escape hatch on queries is for,
it allows the user to basically make our `search_cmip5` and `search_cmip6` functions behave like `search_esgf_like`).

This is a choice that we make.
It is possible to hit the endpoint using multiple different facet names,
e.g. to hit the endpoint with a query that has both `variable` and `variable_id` parameters.
[TODO: get claude to help us clean up the use of parameters vs. facet names vs. argument names so it's consistent]
However, we strongly discourage this.
It is very hard to know what will happen: will the call simply just fail,
will the API take both facets and just give back no results
or will the API just use one or other of the facets, but not both.

As a result, we have set this up so that we don't pass multiple sets of facet names in the one query if you use our high-level interfaces.
Instead, if we need to do a search with multiple sets of facet names, we just make multiple queries.
We do it this way to make the error handling much simpler:
we know that facets can't contradict each other and therefore we don't have to worry about what the API will do with conflicting facet names.
In other words, we provide you with an interface that we are confident has been tested well.
As the user, you can work around this
and create arbitrary queries of the APIs using the `other_terms` property of queries.
You are welcome to use this, but if you do, we cannot guarantee that you will get sensible error handling
because we do not know exactly what the API does in all cases.

[TODO: demo in here. Show that if you try and get variable and variable_id in the same query using our query classes, you can't.
Then show that, if you use `other_terms` or the search API classes directly, you can get them in the same query i.e. you can 'escape' if you want.
Reiterate that, if you do this, you're on your own.]

On top of the issues with passing multiple sets of facets in a single query, there is another issue.
The issue is this: in general, if you hit the search APIs using facet names other than the ones associated with that project, you get no results.
For example, if you search for CMIP6 data using 'variable' as the facet name, rather than 'variable_id', you get no results.
So, in practice there is a coupling between projects and facet names.
However, some projects share the same facet names so it is not a tight or one to one coupling.

We try and set things up with sensible defaults.
This means that, in our default set ups (e.g. default selectors)
and high-level functions
(e.g. the search based on multiple queries that we will add in PR3),
we encode this coupling.
These pieces of code have 'project-aware' logic by default or in-built (which is what we will do in PR3).

This project-awareness introduces a coupling between project and behaviour.
This makes it much easier for us to give correct behaviour and test.
However, it does mean that we introduce a coupling that isn't there in all the search APIs across all projects.
For example, you can hit ESGF1 SOLR indexes and get results back for CMIP6, CMI6Plus and CMIP7 all in one query
[TODO use scripts/demo* to write the demonstration of this here].
Introducing this coupling has some consequences.
For example, we (will) make two or more queries where sometimes one would have been enough
(for example, if you want `tas` across CMIP6 and CMIP7 with ESGF1,
our high-level interfaces would make two queries rather than one).
This is a tradeoff that we are ok with.
The extra queries make maintenance and reliability much easier.
The extra queries do not cost that much in the scheme of things
(this will be particularly true once we have added parallelisation).
In addition, as a user, the low level interfaces still allow you to create a setup
that is optimised to minimise the number of queries, if you want.
[TODO do that demo here i.e. show how we can pass in a query to search that gets results from multiple projects at once
and everything else 'just works', but make clear that, to get this, you have to use the low-level interfaces directly
and know what you're doing (and how it can go wrong, e.g. if you add a single project-specific facet value).
This will include explaining that the low-level is just doing a translation of facet names,
with `other_terms` being passed on exactly as given.
This gives you full control, but also the ability to shoot yourself in the foot
(e.g. if you pass a facet that doesn't exist, you'll just get an error or no results and you have to figure out the error message yourself - we can't give nice error handling for paths that we (deliberately) can't predict).]

[TODO: when we write these docs, also rename QueryCMIP5 to QueryCMIP5Like etc. to make clear
that these query-styles can be used for projects other than the 'named' one i.e they are CMIPX-like, rather than only being used for CMIPX].

## The local database docs

Once we think our local database handling for datasets is stable
(probably once we have actually ingested records from ESGF),
then we should write some docs about how to interact with the database.
Probably include:

1. using the database as a 'plain' database
1. using query objects to drive searches (this will also be a good test to make sure that, if you have a QueryCMIP5 object, that the functions know to look in the CMIP5 table for any CMIP5 specific search facets e.g. product)
