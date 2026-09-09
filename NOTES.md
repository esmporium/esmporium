Delete this file before we merge back into main

- let's talk about `ingest_parsed_documents`. The implementation is good, but the reasons why are subtle so worth looking at
- the shift from doc_parser to response_parser is big, but should be quite straightforward with claude if we get the prompt right I think. We can go through that tomorrow. Probably do that first before going through all the smaller comments in this PR

Prompt draft:

Run `git fetch origin`.

Then compare the changes in this branch to `origin/results-to-database`.

I want you to focus on the changes related to changing doc_parser to a result_parser.
Please consider those comments and either make a plan for moving all relevant result parsing behaviour onto a new result_parser class or describe why you wouldn't do this.
If you do agree we should switch to result_parser, note that this result_parser class will replace the existing idea of a doc_parser (we don't need to keep both result_parser and doc_parser).
