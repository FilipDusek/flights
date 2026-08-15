<div align="center">

# ✈️ fast-flights (v3.0rc1)

The fast and strongly-typed Google Flights scraper (API) implemented in Python.
Based on Base64-encoded Protobuf string.

[**Documentation (v2)**](https://aweirddev.github.io/flights) • [Issues](https://github.com/AWeirdDev/flights/issues) • [PyPi (v3.0rc0)](https://pypi.org/project/fast-flights/3.0rc0/)

```haskell
$ pip install fast-flights
```

</div>

## At a glance
```python
from fast_flights import (
    FlightQuery,
    Passengers, 
    create_query, 
    get_flights
)

query = create_query(
    flights=[
        FlightQuery(
            date="YYYY-MM-DD",   # change the date
            from_airport="MYJ",  # three-letter name
            to_airport="TPE",    # three-letter name
        ),
    ],
    seat="economy",  # business/economy/first/premium-economy
    trip="one-way",  # multi-city/one-way/round-trip
    passengers=Passengers(adults=1),
    language="zh-TW",
)
res = get_flights(query)
```

## Explore (destination inspiration)

The Google Flights **Explore** map ("from Copenhagen to anywhere, 1-week trip
in the next 6 months") as structured data — same internal RPC the map uses,
no browser needed:

```python
from fast_flights import explore

result = explore("CPH", "Europe", month=9, trip_length="weekend", max_price=1500)
for d in result.destinations[:5]:
    print(d.name, d.price, d.currency, d.depart_date, d.return_date, d.flights_url)
print(result.explore_url)  # reopen this exact search in the browser
```

Or from the CLI (installed as a second console script, `flights-explore`):

```console
$ flights-explore CPH                              # anywhere, next 6 months, 1 week
$ flights-explore Copenhagen Europe -m sep -l weekend
$ flights-explore CPH Thailand -d 2026-11-10 -r 2026-11-24 --currency EUR
$ flights-explore CPH --max-price 1500 --stops 0 --airlines STAR_ALLIANCE --json
$ flights-explore CPH --bounds 70,25,54,4          # arbitrary geographic box
```

Origins and destinations are freeform (IATA codes, cities, countries, regions,
continents — resolved through Google's own autocomplete). Each result carries
the cheapest found itinerary (price, carrier, stops, duration, dates), plus a
deep link to the regular flight search for those exact dates; the query itself
gets an `explore_url` that reopens the same search on the map. Filters: specific
dates or flexible (month + weekend/week/two-weeks), max stops, airlines and
alliances, price cap, carry-on bags, cabin, passengers, one-way, flights-only,
and a lat/lng bounding box. Hotel prices shown in the web UI are not included
(separate RPC).

## Integrations
If you'd like, you can use integrations.

Bright data:

```python
from fast_flights import get_flights
from fast_flights.integrations import BrightData

get_flights(..., integration=BrightData())
```

## What's new
- `v2.0` – New (much more succinct) API, fallback support for Playwright serverless functions, and [documentation](https://aweirddev.github.io/flights)!
- `v2.2` - Now supports **local playwright** for sending requests.
- `v3.0rc0` - Uses Javascript data instead.

## Contributing
Contributing is welcomed! A few notes though:
1. please no ai slop. i am not reading all that.
2. one change at a time. what your title says is what you've changed.
3. no new dependencies unless it's related to the core parsing.
4. really, i cant finish reading all of them, i have other projects and life to do. really sorry

***

## How it's made

The other day, I was making a chat-interface-based trip recommendation app and wanted to add a feature that can search for flights available for booking. My personal choice is definitely [Google Flights](https://flights.google.com) since Google always has the best and most organized data on the web. Therefore, I searched for APIs on Google.

> 🔎 **Search** <br />
> google flights api

The results? Bad. It seems like they discontinued this service and it now lives in the Graveyard of Google.

> <sup><a href="https://duffel.com/blog/google-flights-api" target="_blank">🧏‍♂️ <b>duffel.com</b></a></sup><br />
> <sup><i>Google Flights API: How did it work & what happened to it?</i></b>
>
> The Google Flights API offered developers access to aggregated airline data, including flight times, availability, and prices. Over a decade ago, Google announced the acquisition of ITA Software Inc. which it used to develop its API. **However, in 2018, Google ended access to the public-facing API and now only offers access through the QPX enterprise product**.

That's awful! I've also looked for free alternatives but their rate limits and pricing are just 😬 (not a good fit/deal for everyone).

<br />

However, Google Flights has their UI – [flights.google.com](https://flights.google.com). So, maybe I could just use Developer Tools to log the requests made and just replicate all of that? Undoubtedly not! Their requests are just full of numbers and unreadable text, so that's not the solution.

Perhaps, we could scrape it? I mean, Google allowed many companies like [Serpapi](https://google.com/search?q=serpapi) to scrape their web just pretending like nothing happened... So let's scrape our own.

> 🔎 **Search** <br />
> google flights ~~api~~ scraper pypi

Excluding the ones that are not active, I came across [hugoglvs/google-flights-scraper](https://pypi.org/project/google-flights-scraper) on Pypi. I thought to myself: "aint no way this is the solution!"

I checked hugoglvs's code on [GitHub](https://github.com/hugoglvs/google-flights-scraper), and I immediately detected "playwright," my worst enemy. One word can describe it well: slow. Two words? Extremely slow. What's more, it doesn't even run on the **🗻 Edge** because of configuration errors, missing libraries... etc. I could just reverse [try.playwright.tech](https://try.playwright.tech) and use a better environment, but that's just too risky if they added Cloudflare as an additional security barrier 😳.

Life tells me to never give up. Let's just take a look at their URL params...

```markdown
https://www.google.com/travel/flights/search?tfs=CBwQAhoeEgoyMDI0LTA1LTI4agcIARIDVFBFcgcIARIDTVlKGh4SCjIwMjQtMDUtMzBqBwgBEgNNWUpyBwgBEgNUUEVAAUgBcAGCAQsI____________AZgBAQ&hl=en
```

| Param | Content | My past understanding |
|-------|---------|-----------------------|
| hl    | en      | Sets the language.    |
| tfs   | CBwQAhoeEgoyMDI0LTA1LTI4agcIARID… | What is this???? 🤮🤮 |

I removed the `?tfs=` parameter and found out that this is the control of our request! And it looks so base64-y.

If we decode it to raw text, we can still see the dates, but we're not quite there — there's too much unwanted Unicode text.

Or maybe it's some kind of a **data-storing method** Google uses? What if it's something like JSON? Let's look it up.

> 🔎 **Search** <br />
> google's json alternative

> 🐣 **Result**<br />
> Solution: The Power of **Protocol Buffers**
> 
> LinkedIn turned to Protocol Buffers, often referred to as **protobuf**, a binary serialization format developed by Google. The key advantage of Protocol Buffers is its efficiency, compactness, and speed, making it significantly faster than JSON for serialization and deserialization.

Gotcha, Protobuf! Let's feed it to an online decoder and see how it does:

> 🔎 **Search** <br />
> protobuf decoder

> 🐣 **Result**<br />
> [protobuf-decoder.netlify.app](https://protobuf-decoder.netlify.app)

I then pasted the Base64-encoded string to the decoder and no way! It DID return valid data!

![annotated, Protobuf Decoder screenshot](https://github.com/AWeirdDev/flights/assets/90096971/77dfb097-f961-4494-be88-3640763dbc8c)

I immediately recognized the values — that's my data, that's my query!

So, I wrote some simple Protobuf code to decode the data.

```protobuf
syntax = "proto3"

message Airport {
    string name = 2;
}

message FlightInfo {
    string date = 2;
    Airport dep_airport = 13;
    Airport arr_airport = 14;
}

message GoogleSucks {
    repeated FlightInfo = 3;
}
```

It works! Now, I won't consider myself an "experienced Protobuf developer" but rather a complete beginner.

I have no idea what I wrote but... it worked! And here it is, `fast-flights`.

***

<div align="center">

(c) 2024-2026 AWeirdDev, and all the awesome people

</div>
