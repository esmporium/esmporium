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

## The local database docs

Once we think our local database handling for datasets is stable
(probably once we have actually ingested records from ESGF),
then we should write some docs about how to interact with the database.
Probably include:

1. using the database as a 'plain' database
1. using query objects to drive searches (this will also be a good test to make sure that, if you have a QueryCMIP5 object, that the functions know to look in the CMIP5 table for any CMIP5 specific search facets e.g. product)
