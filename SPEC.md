# Chipload Specification v0.1.0

Chipload is a simple, shared way to write down what a machine shop has and what each machine can do, so any piece of software can read the same description.

## The problem we're solving

What a shop knows about itself is scattered. The machine list is in a spreadsheet, what each machine can really do is in people's heads, connection details are in vendor software, and part data is in the ERP.

Every new tool, whether a monitoring dashboard, a scheduler or an MES, needs that same knowledge. So someone types it in again, and each copy slowly drifts out of date.

Machines also describe themselves differently. One mill reports `ACTIVE`, another `RUNNING`, a third a number in a register. Answering "is the mill running?" takes custom code for every brand.

Chipload fixes both problems:

- **One description of the shop.** A single file says what the shop has, and every tool reads it.
- **One vocabulary for machines.** Every machine's status and abilities are described the same way, whatever its brand or protocol.

&#91;embedded content: The problem · before and after Chipload\]

Without a shared description, every tool needs its own link to every machine. With Chipload, each needs only one.

## What Chipload is

Chipload is a file format, not software. You write a shop file in YAML or JSON, keep it with your other documents, and point your tools at it. Version 0.1.0 defines only how to describe the data. How tools send it to each other comes later.

A shop file uses four building blocks:

| Block | What it is | Example |
| --- | --- | --- |
| Shop | The business that owns the equipment | Acme Precision |
| Site | A physical location | Main plant |
| Resource | Anything work uses or happens in: machines, cells, benches, tools, fixtures, gauges | `mill-01` |
| Capability | A named set of things a resource can report and do | `shop.status@1`: reports whether it is running |

## A complete example

This is a whole, valid shop file. It describes one plant with a cell holding a mill and a deburr bench, plus a CMM.

```yaml
chipload: "0.1"

shop:
  id: acme
  name: Acme Precision

sites:
  - id: main
    name: Main plant
    timeZone: America/Chicago

resources:
  - id: cell-1
    type: cell
    name: Cell 1
    site: main

  - id: mill-01
    type: machine
    name: Vertical mill
    site: main
    parent: cell-1
    details:
      manufacturer: Example Machines
      model: VM-500
      serialNumber: "A12345"
    capabilities: [shop.status@1, shop.programs@1, shop.jobs@1]

  - id: deburr-bench
    type: station
    name: Deburr bench
    site: main
    parent: cell-1
    capabilities: [shop.manual-work@1]

  - id: cmm-01
    type: gauge
    name: CMM
    site: main
    capabilities: [shop.status@1, shop.inspection@1]
```

Any tool reading this file knows the mill exists, that it sits in Cell 1, and that it can report its status, accept programs and run jobs.

## Describing a shop

A shop file has four required parts and one optional part.

| Part | Required | What goes in it |
| --- | --- | --- |
| `chipload` | Yes | The spec version, `"0.1"` |
| `shop` | Yes | The shop's `id` and `name` |
| `sites` | Yes | At least one site |
| `resources` | Yes | Everything the shop has |
| `capabilities` | No | Your own capability definitions (next section) |

### Sites

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | Yes | Short permanent name |
| `name` | Yes | Display name |
| `timeZone` | Yes | Time zone name, such as `America/Chicago` |
| `address` | No | Postal address as text |

### Resources

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | Yes | Short permanent name. Never changes, even when the machine moves |
| `type` | Yes | `area`, `cell`, `station`, `machine`, `tool`, `fixture`, `gauge`, `utility` or `other` |
| `name` | Yes | Display name. Can change any time |
| `site` | Yes | The site it is in |
| `parent` | No | The resource it sits inside, such as its cell |
| `capabilities` | No | What it can report and do |
| `details` | No | `manufacturer`, `model`, `serialNumber` and any other facts you want to record |
| `status` | No | `active` (the default), `inactive` or `retired` |

Two things to know:

- Parents form a simple tree. A resource cannot end up inside itself.
- A replacement machine gets a new `id`, even in the same spot. Its history stays separate from the old one.

## Capabilities

A capability is a promise: "this resource can report these things and accept these requests." Its name has three parts. In `shop.status@1`, `shop` marks it as built in, `status` is the name and `1` is the version. A capability that changes incompatibly gets a new version, such as `@2`, so `@1` never changes meaning.

Version 0.1.0 has five built-in capabilities:

| Capability | Reports | Can be asked to |
| --- | --- | --- |
| `shop.status@1` | Connection: `online`, `offline`, `unknown`. Mode: `manual`, `automatic`, `setup`, `maintenance`, `unknown`. Activity: `idle`, `running`, `held`, `stopped`, `faulted`, `unknown`. Active alarms | Nothing |
| `shop.programs@1` | The selected program | Load, check and select a program |
| `shop.jobs@1` | The active job | Prepare, start, hold, resume and cancel a job |
| `shop.manual-work@1` | Nothing | Record that a person finished a task |
| `shop.inspection@1` | Calibration: `valid`, `due`, `expired`, `unknown` | Measure a part |

The last column describes ability only. Version 0.1.0 does not define how requests are sent, and nothing in a shop file ever starts a machine.

### Your own capabilities

Anything the built-ins don't cover, you define with your own prefix:

```yaml
capabilities:
  - name: acme.laser-marking@1
    description: Marks part numbers with a fiber laser
    reports:
      - name: laser-power
        unit: W
    actions:
      - name: mark
        description: Marks the given text on the loaded part
```

Then list `acme.laser-marking@1` on a resource like any built-in capability. Each report has a `name` and either a `unit` or a list of allowed `values`. Each action has a `name` and a `description`.

## Reporting what a machine is doing

The shop file says what a machine can report. A status report carries one actual value at one moment, in the same shape for every brand:

```json
{
  "resource": "mill-01",
  "report": "shop.status@1#activity",
  "value": "running",
  "time": "2026-10-20T14:04:05Z",
  "maxAge": "PT5S",
  "source": "controller",
  "native": "ACTIVE"
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `resource` | Yes | Which resource |
| `report` | Yes | Capability and report name, joined by `#` |
| `value` | Yes | The value, or `null` when it is unavailable |
| `time` | Yes | When the value was true, with time zone |
| `maxAge` | Yes | How long the value can be trusted. `PT5S` means 5 seconds |
| `source` | Yes | `controller`, `sensor`, `person`, `calculated` or `simulated` |
| `native` | No | What the machine itself said, kept for reference |

A report older than its `maxAge` is stale and must not be treated as current. Simulated data always says `simulated`, so test data can never pass for real data.

## Ground rules

1. **Describing is not doing.** Nothing in a shop file or a status report starts, stops or changes a machine.
2. **IDs are permanent.** Use lowercase letters, digits and hyphens, up to 64 characters. An ID is unique within its list and is never reused.
3. **Names are for people, IDs are for software.** Never match two things just because their names agree.
4. **Measurements carry units.** Write them as `{value: "760", unit: mm}`, using standard UCUM unit codes such as `mm`, `kg` and `kPa`. The number is quoted so it is never rounded.
5. **Times carry a time zone,** such as `2026-10-20T14:04:05Z`.
6. **Unknown is an honest answer.** Report `unknown` or `null` rather than guess.
7. **Extras have a home.** Extra facts about a resource go in `details`, and new abilities go in your own prefixed capabilities. Any other unrecognized field is an error, so typos get caught.
8. **YAML and JSON are equal.** A file means the same in either. In YAML, quote values that could be misread as numbers, such as `"0.1"` or a serial number.