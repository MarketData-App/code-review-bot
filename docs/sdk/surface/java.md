## 8. The public surface of this SDK

The surface is every `public` and `protected` type, method, constructor and
field under `src/main/java`. The artifact is published to Maven Central as
`app.marketdata`, and it is idiomatic from both Java and Kotlin, so a change
must hold for both callers.

Java breaks in two ways, and they are not the same. Say which one you found:

- **source compatibility**: existing caller code stops compiling
- **binary compatibility**: existing compiled callers stop linking, and fail at
  runtime with `NoSuchMethodError` even though the source would compile

Breaking, for this SDK:

- a `public` or `protected` type, method, constructor or field removed or
  renamed
- a parameter type or a return type changed. Changing `dte(String)` to
  `dte(DteFilter)` breaks every caller, even when the API change behind it was
  purely additive
- a parameter type widened in place, which keeps source compatibility and
  breaks binary compatibility. Adding an overload keeps both
- an abstract method added to an interface or an abstract class, unless it has a
  `default` implementation
- visibility narrowed, a method made `final`, or a class made `final`
- a checked exception added to a `throws` clause
- an enum constant removed, or reordered when callers depend on `ordinal()`

Not breaking: a new overload, a new `default` interface method, a new enum
constant at the end, a widened return type on a `final` method.

The version comes from the `sdkVersion` Gradle property at release time, not
from a file in the tree. The bump is therefore declared in `CHANGELOG.md` and in
the pull request description.
