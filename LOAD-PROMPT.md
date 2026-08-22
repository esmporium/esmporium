Eventually we're going to start handling large amounts of data. Here, large means hundreds of GBs per dataset, so when multiple datasets are processed we're looking at TBs.

This handling/processing involves multiple steps. Because of the data volume and computation time, re-running every step every time doesn't make sense. We need some sort of caching/dependency checking, like what `make` and a `Makefile` can provide. A key part of this is that the results of different steps can require custom serialisation so, whatever we end up using has to be able to handle this serialisation sensibly and, ideally, work nicely within the python universe.

I would like to understand the options for implementing this. Here are some options I have considered.

`make` and `Makefile`s. These almost immediately become unwieldy, and `make` syntax is extremely hard to understand. I assume this is not a good option for anything mildly complex.

prefect (https://www.prefect.io/). This seems to have all of the job definition, dependency and caching features, plus a bunch of other features I don't need. The nice thing about prefect is that you just decorate python functions so things 'just work' as if you were re-running in memory every time. The pain point with prefect is caching with file outputs: I have never been able to understand how this is meant to work with prefect and the caching has never really worked as I hoped, particularly when the cache has to considered the checksum of input files (which I don't think is an inbuilt feature).

`snakemake`. To me, this is just `make` but in python i.e. it suffers from all the same problems as `make`, the syntax is just easier to understand.

pydoit (https://pydoit.org/index.html). I have used this before. In the end, I found it's way of setting things up cumbersome (particularly compared to prefect) and it ended up getting in the way rather than helping. Maybe I wasn't setting it up correctly. It did have the 'cache on input file hash' behaviour which I miss with prefect, but caching was still tricky and it would still sometimes get it wrong.

Rolling my own solution. This is currently what I am leaning towards. Caching and task management is a really difficult general problem. I know the use case, so can write something specific to that use case, which will make the problem more tractable. My current instinct is to have a basic SQL database for storing job information (last run, input hashes, source, output hashes etc.) and then just allow the user to specify serialisers and deserialisers to support saving of intermediate output in a way that suits their data, rather than only supporting serialisation to e.g. JSON or forcing the user to wrap their own serialisation/deserialisation stuff around their pure python functions (which is what I ended up doing with prefect, because I couldn't figure out how to get it to handle intermediate outputs properly).

Please also do some web searching to find any other options that seem good.

I would like you to do a deep dive on this. Go and read the source code if you need to. Look at the pros and cons of each option. Ask me any clarifying questions you need to help understand the shape of the problem. Then please write your conclusions/summary to a file called `LOAD-CLAUDE-INVESTIGAION.md`.
