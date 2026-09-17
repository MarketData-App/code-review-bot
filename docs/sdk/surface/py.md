## 8. The public surface of this SDK

The surface is every name in `src/marketdata/` that does not start with `_`,
plus everything re-exported from `src/marketdata/__init__.py`. The package is
installed by customers as `marketdata-sdk-py`.

A signature here includes the parameter names, because callers pass them by
keyword. Renaming a parameter breaks callers even when the position is the same.

Breaking, for this SDK:

- a name removed from `__init__.py`, even when the module-level name survives
- a parameter renamed, removed, reordered, or moved from optional to required
- a type hint narrowed on a parameter, or widened on a return value
- a changed default
- a resource method that starts to raise where it used to return
  `MarketDataClientErrorResult`, or that raises a different
  `BaseMarketdataException` subclass
- a field removed from a Pydantic model or a `TypedDict`, or made required
- an `Enum` member removed or its value changed

Not breaking: a new keyword argument with a default, a new model field that is
optional, a widened accepted type on a parameter.

A removal also carries a test that pins the absence, so the surface cannot come
back quietly.
