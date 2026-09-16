## 8. The public surface of this SDK

The surface is every `public` and `protected` type and member under
`src/MarketDataApp`. The package is published to NuGet. `src/MarketDataApp.Tests`
and `src/MarketDataApp.IntegrationTests` are not surface.

Breaking, for this SDK:

- a `public` or `protected` type, method, property, event or field removed or
  renamed
- a parameter or return type changed
- a default parameter value changed. The value is compiled into the caller, so
  existing binaries keep the old default and new builds get the new one
- a parameter added, even with a default, when callers may be using named
  arguments after it, or when the method is used in an expression tree
- an abstract member added to a public abstract class or an interface without a
  default implementation
- visibility narrowed, or a class or member made `sealed`
- a different exception type thrown for the same failure
- an enum member removed, or its value changed

Not breaking: a new overload, a new type, a new enum member at the end of the
list, a new interface with a default implementation.

The version comes from the project properties at release time. Declare the bump
in `CHANGELOG.md` and in the pull request description.
