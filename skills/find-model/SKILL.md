---
name: find-model
description: 'Find and compare LLMs in the OpenRouter catalog: fuzzy-match model names, filter by vendor, price, context, modality or tool support, sort by price or features, list option values. 选模型/换模型/最便宜的模型'
---

# Find a model

Query the OpenRouter model catalog that Conic syncs every hour into `data/openrouter_models.json`. Use `scripts/find_model.py` (Python 3, standard library only) instead of reading the JSON yourself: the file is about 430 KB.

The catalog is located automatically: `--catalog PATH`, then the `CONIC_MODEL_CATALOG` environment variable, then `data/openrouter_models.json`, then `conic/data/openrouter_models.json`, all relative to the current directory. If none exists the script says which paths it tried.

## Commands

```
python scripts/find_model.py find [QUERY] [options]   # fuzzy search, filter, sort
python scripts/find_model.py options                  # every value the options accept
python scripts/find_model.py show QUERY               # full catalog record
```

Every command also accepts `--catalog PATH` and `--json` (machine-readable output).

## `find` options and their values

| Option | Value | Effect |
| --- | --- | --- |
| `QUERY` | any text | Fuzzy match on slug, model name and display name. Works with a missing vendor prefix, a partial name, several words (`"deepseek flash"`) or a small typo (`deepseek-v4-flsh`). |
| `--min-score` | number 0-100, default 30 | Fuzzy threshold. Raise it (60+) for stricter matches. |
| `--vendor` | a vendor name, repeatable | Only these vendors, for example `--vendor openai --vendor anthropic`. |
| `--tools` | flag | Only models that support tool calling. Required for agents. |
| `--input-modality` | `text`, `image`, `file`, `video`, `audio`, repeatable | Model must accept all listed input types. |
| `--output-modality` | `text`, `image`, `audio`, repeatable | Model must produce all listed output types. |
| `--param` | a supported request parameter, repeatable | Model must support all of them, for example `--param reasoning --param structured_outputs`. |
| `--min-context` | integer tokens | Minimum context window, for example `--min-context 200000`. |
| `--max-price-in` | USD per 1M input tokens | Upper bound on input price. |
| `--max-price-out` | USD per 1M output tokens | Upper bound on output price. |
| `--free` | flag | Only models whose input and output price are both 0. |
| `--paid` | flag | Only models with a positive price. |
| `--no-alias` | flag | Hide `~vendor/name-latest` aliases. |
| `--no-variants` | flag | Hide `:batch` and `:free` variants. |
| `--exclude` | text, repeatable | Drop slugs containing this text. |
| `--sort` | see below | Sort key. Default is `match` when a query is given, otherwise `name`. |
| `--reverse` | flag | Flip the default direction of the sort key. |
| `--limit` | integer, default 15 | Number of rows shown. |

### `--sort` values

| Key | Order by | Default direction |
| --- | --- | --- |
| `price_in` | input price per 1M tokens | cheapest first |
| `price_out` | output price per 1M tokens | cheapest first |
| `context` | context window | largest first |
| `params` | number of supported request parameters | most first |
| `modalities` | number of input plus output modalities | most first |
| `name` | slug | A to Z |
| `vendor` | vendor, then slug | A to Z |
| `match` | fuzzy match score, needs a query | best first |

Models with a variable price (the `openrouter/auto` routers) have no fixed price: they are excluded by `--max-price-*`, `--paid` and `--free`, and always listed last when sorting by price.

## All values are read from the catalog

The lists above are stable, but vendors and supported parameters change with every sync. Run `options` to get the current sets with counts:

```
python scripts/find_model.py options
```

It prints the model count, aliases, variants, every vendor, every input and output modality, every supported parameter, the sort keys, the context length range, the paid price ranges, and the number of free, tool-capable and variable-price models. At the time of writing the parameters include `tools`, `tool_choice`, `reasoning`, `reasoning_effort`, `include_reasoning`, `structured_outputs`, `response_format`, `temperature`, `top_p`, `top_k`, `seed`, `stop`, `max_tokens`, `logprobs`, `web_search_options` and more.

## Choosing well

- There is no benchmark data in this catalog, so "capability" here means measurable features: context window (`context`), the number of supported parameters (`params`), modalities (`modalities`), and tool or reasoning support. Do not claim one model is smarter than another from this data. If the user needs quality rankings, say the catalog cannot answer that and suggest an external leaderboard.
- Price and size do not reliably indicate strength. Within one vendor, newer cheaper models often beat older expensive ones.
- For an agent, always add `--tools`, and usually `--no-variants --no-alias` to get one row per real model.
- Aliases (`~vendor/name-latest`) point at a real model, shown as `alias -> real_model`. Use the `real_model` (the part after `->`) when reporting a concrete model.
- Model names are written `vendor/model`, for example `deepseek/deepseek-v4-flash-0731`. Quote them exactly when telling the user which model to use.

## Examples

```
python scripts/find_model.py find "deepseek flash" --no-variants
python scripts/find_model.py find --tools --min-context 200000 --paid --no-alias --no-variants --sort price_out --limit 5
python scripts/find_model.py find --input-modality image --param reasoning --sort context
python scripts/find_model.py find --vendor anthropic --no-variants --sort price_in --reverse
python scripts/find_model.py find --free --tools
python scripts/find_model.py show deepseek-v4-flash-0731
```
