# REVIEW.md

I reviewed the starter against the API contract and the MongoDB data model.

| # | Where | What is wrong | How it shows up | How it is handled now |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It uses the timestamp's current date/time zone instead of the employee's IST attendance date, which breaks overnight shifts. | A punch just after midnight on a 22:00–06:00 shift is compared with the wrong shift start. | Convert instants to IST and resolve the attendance date before finding the shift start. |
| 2 | `compute_late_minutes` | The starter does not consistently truncate incoming instants to whole seconds. | A fractional-second punch can change the grace-period boundary. | Epoch-millisecond inputs are validated and stored at whole-second precision. |
| 3 | `compute_work_hours` | Python `round()` uses ties-to-even, not the required half-up rounding. | A value exactly halfway between two hundredths can round down unexpectedly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It does not handle overnight shift ends on the next day and does not apply the 30-minute minimum. | A 06:40 punch-out for a 22:00–06:00 shift can get a wrong duration. | Resolve the correct shift end date and return overtime only when it reaches 30 whole minutes. |
| 5 | `/health` | It always returns success without checking MongoDB. | The app reports ready while the database is unavailable. | Run a MongoDB ping and return 503 on a database error. |
| 6 | Employee model | Input formats, lengths, and extra fields are not validated. | Malformed employee codes, shift times, and dates can be stored. | Add Pydantic validation and reject unexpected request fields. |
| 7 | `create_employee` | Check-then-insert can create duplicates under concurrent requests. | Two simultaneous requests can both pass the initial lookup. | Create a unique `emp_code` index and translate duplicate-key errors to 409. |
| 8 | `create_employee` | `created_at` is a naive local datetime. | Returned timestamps can depend on server time-zone settings. | Store an aware UTC timestamp and serialize instants as epoch milliseconds. |
| 9 | `list_employees` | Pagination skips `page * page_size` rows, and `total` ignores the department filter. | Page 1 starts too late and totals disagree with the filtered list. | Use `(page - 1) * page_size`, filtered counts, stable sorting, and bounded page sizes. |
| 10 | `punch_in` | It dereferences an employee without checking that the employee exists. | An unknown code can produce a 500 instead of 404. | Check for the employee first. |
| 11 | `punch_in` | The timestamp is interpreted in the server's local zone and the date is not calculated using the overnight-shift rule. | UTC timestamps and after-midnight punches can be assigned to the wrong day. | Convert to IST and derive the attendance date from the employee shift. |
| 12 | `punch_in` | A lookup before insert is not safe under concurrency; status and timestamp validation are missing. | Duplicate requests can both insert, and invalid status values can be saved. | Validate the request and use a unique `(emp_code, date)` index as the concurrency guarantee. |
| 13 | `/attendance` | It reads and sorts the entire collection in Python. | Memory and response time grow with a 100,000-record collection. | Apply filters, sort, count, skip, and limit in MongoDB with supporting indexes. |
| 14 | `/attendance` | Sorting omits the `emp_code` tie-breaker, and Mongo `_id` is exposed. | Results can move between pages and internal IDs leak into the response. | Sort by date descending then code ascending, and omit `_id`. |
| 15 | Request/response timestamps | The starter mixes seconds, local naive datetimes, and BSON dates. | Clients can receive inconsistent timestamp values or invalid values can pass. | Validate epoch milliseconds at the API boundary and serialize BSON datetimes back to milliseconds. |
| 16 | Missing routes | Punch-out, correction, analytics, and explain endpoints are absent. | The contract's corresponding requests return 404. | Implement the routes in `main.py`. |
| 17 | Punch-out and correction | A read-then-write update alone can let concurrent requests overwrite each other. | Two punch-outs or corrections can both appear successful or lose audit history. | Use conditional MongoDB updates; corrections compare the previous record state before appending history. |
| 18 | Analytics | Fetching records into Python would not scale and misses headcount-only employees. | Large months are slow; employees with no logs disappear. | Use MongoDB aggregation pipelines and start the department summary from employees. |

I kept the starter's basic FastAPI/PyMongo structure because it fits the requested stack. I also kept calendar dates as `YYYY-MM-DD` strings in MongoDB; they are dates, not instants, so converting them to timestamps would add unnecessary time-zone ambiguity.

## Additional data-model edge cases handled

The supplied data model permits `history` and `half_day` to be absent on older records. Regularization treats those as an empty history and `false`, respectively, and its compare-and-swap filter distinguishes a missing `history` field from an explicitly empty array. The department trend is seeded from `employees` so it can return calendar days even when the department has no attendance logs for those dates.
