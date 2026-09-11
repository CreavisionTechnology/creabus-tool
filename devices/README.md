# Device profile reference

One file describes one device completely. Drop it in this folder, press
**Rescan** in the UI, and it appears in the dropdown. `.yaml`, `.yml` and
`.json` are all accepted (JSON needs no PyYAML).

Start from [`_template.yaml`](_template.yaml), which uses every option below
once with comments.

```yaml
device:
  ...device level settings...
registers:
  - ...one entry per register...
```

## `device`

| key | default | meaning |
| --- | --- | --- |
| `name` | file name | shown in the UI title and by "report server id" |
| `model` | `""` | appended to the name in brackets |
| `vendor` | `""` | informational |
| `description` | `""` | one line, logged when the profile loads |
| `unit_id` | `1` | the Modbus unit / slave id this device answers on |
| `accept_any_unit_id` | `false` | `true` answers whatever unit id is asked for; `false` replies `0x0B` to the others |
| `default_interval` | `30` | seconds between value changes for registers that do not set their own |
| `word_order` | `big` | `big` = high 16 bit word first (Eastron, most PLCs). `little` = low word first, the "CDAB" layout many inverters use |
| `byte_order` | `big` | byte order inside each 16 bit word |
| `gap_policy` | `zero` | `zero`: addresses with no register read back as 0, so block reads spanning gaps work. `exception`: reply `0x02 illegal data address` |

## `registers`

Each entry is a mapping. `name` and `address` are required; everything else has
a default.

| key | default | meaning |
| --- | --- | --- |
| `name` | – | identifier, letters/digits/underscore only. This is the name other registers use in expressions |
| `label` | from `name` | display name in the UI and the log |
| `table` | `input` | `input`, `holding`, `coil` or `discrete` |
| `address` | – | protocol address, 0 based. `0x000C` and `12` are the same thing |
| `type` | `float32` | see **Types** |
| `unit` | `""` | display only (`V`, `kWh`, ...) |
| `mode` | `random` | see **Modes** |
| `min` / `max` | – | limits for the generated value; also clamp expressions and accumulators |
| `step` | `(max-min)/20` | largest change per update, `walk` mode |
| `start` / `value` | midpoint | value at start up |
| `interval` | `default_interval` | seconds between updates for this register |
| `decimals` | – | round to this many decimals |
| `scale` | `1.0` | engineering value = raw register × scale. Use `0.1` when the device stores tenths |
| `expression` | – | required by `mode: expression` |
| `rate` | – | required by `mode: accumulator`, units per second |
| `wrap` | `false` | accumulators roll over to `min` at `max` instead of sticking |
| `length` | – | characters, required for `type: string` |
| `writable` | `true` for holding/coil | whether a client may write it |

### Addresses

`address` is the protocol address as it goes on the wire. Manuals often quote
the "documentation" address instead: input register `0x0000` is printed as
`30001`, holding register `0x0000` as `40001`. The UI shows both.

Registers must not overlap — a `float32` at `0x0000` occupies `0x0000` and
`0x0001`, so the next one starts at `0x0002`. The loader refuses a profile with
overlapping registers and tells you which two collide.

## Types

| type | registers | notes |
| --- | --- | --- |
| `float32` | 2 | IEEE-754, what every Eastron meter uses |
| `float64` | 4 | |
| `int16` / `uint16` | 1 | |
| `int32` / `uint32` | 2 | |
| `int64` / `uint64` | 4 | |
| `string` | `length/2` | ASCII, null padded, needs `length` |
| `bool` | – | implied for `coil` and `discrete` |

Integer types are rounded and clamped to the range of the type, so `scale` plus
`min`/`max` cannot produce an out-of-range write.

## Modes

### `walk` — a random walk
Moves by at most `step` per update and stays between `min` and `max`. This is
what you want for live measurements: readings drift the way a real meter's do
instead of jumping across the whole range every interval.

```yaml
mode: walk
min: 226.0
max: 243.0
start: 236.7
step: 0.8
interval: 5
```

### `random` — a fresh sample
Uniform in `[min, max]` every interval. Fine for values with no continuity, or
for coils and discrete inputs (50/50).

### `fixed` — never changes
Identity, configuration, serial numbers, anything a client writes. Combine with
`writable: true` on a holding register and the written value is kept.

### `expression` — derived from other registers
The value is computed, so the device stays internally consistent. Available
inside an expression:

* every register, by `name`
* maths: `sqrt sin cos tan asin acos atan atan2 degrees radians log log10 exp
  floor ceil fmod hypot pi e inf`
* general: `abs min max round int float pow sum len bool`
* random: `uniform(a, b)`, `gauss(mu, sigma)`, `randint(a, b)`, `choice(...)`
* `t` — seconds since the server started, and `now` — unix time

```yaml
- name: active_power
  mode: expression
  expression: "voltage * current * power_factor"
```

Referring to the register's **own** name gives you a peak hold:

```yaml
- name: max_demand
  mode: expression
  expression: "max(max_demand, demand)"
  start: 0.0
```

Expression registers are evaluated in dependency order, and recompute as soon
as anything they read changes as well as on their own `interval`, so a derived
value never lags behind its inputs.

### `accumulator` — a counter that only moves one way
`rate` is an expression giving the change per second. It is integrated over the
real elapsed time, so the total is right whatever `interval` you choose.

```yaml
- name: import_active_energy
  mode: accumulator
  rate: "active_power / 3600000.0"    # W -> kWh per second
  start: 12345.678
  max: 99999999.0
  interval: 10
```

Add `wrap: true` to roll over at `max` the way a real meter's display does.

## Timing

`default_interval` at device level sets the pace; any register can override it
with `interval`. The simulation ticks four times a second, so an interval is
honoured to within about 250 ms. The Registers tab shows the countdown to each
register's next change.

## Errors

The profile is validated on load and the UI tells you exactly what is wrong —
unknown type or mode, a missing `min`/`max` on a random register, an
`expression` mode without an expression, overlapping addresses, a duplicate
name, an interval of zero. Nothing loads partially: either the whole profile is
good or the previous device stays.

An expression that fails at runtime (a division by zero, a typo in a register
name) does not stop the server: the register keeps its last value, the row turns
red in the Registers tab, and the reason is logged once.

## A warning about expressions

`expression` and `rate` carry code, and profiles get shared. So an expression
is parsed and checked against an allowlist of syntax before anything runs:
arithmetic, comparisons, a conditional, and calls to the helpers listed above.

Not allowed, and refused when the profile loads rather than when the value is
wanted: attribute access (`a.b`), indexing (`a[b]`), lambdas, comprehensions,
f-strings, assignment, and any call to anything not in that list. Attribute
access is the one that matters — it is the first step of every escape from an
`eval`, so removing it removes the class.

An enormous literal exponent is refused too (`10**10**8` is valid Python and
would tie up the simulation thread for a very long time).

This is a real boundary, not a warning. If you find a way past it, that is a
vulnerability and [SECURITY.md](../SECURITY.md) says how to report it.
