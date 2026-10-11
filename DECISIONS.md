# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique. Employee indexes support department-sorted listing and department/join-date headcount. Attendance has a unique `(emp_code, date)` index, a date/code listing index, a status/date/code index, a date/late leaderboard index, and an employee/punch-in index for finding the latest punch. Extra indexes cost storage and slow writes, so I focused on contract queries.

2. **Punch-in race.** Two requests can pass the employee lookup, so that check alone is not enough. The unique attendance index decides the winner: one insert succeeds with 201 and the duplicate-key error becomes 409.

3. **Ties.** The leaderboard uses MongoDB's `$rank`, not row positions. If two employees tie at rank 10, both are returned for `limit=10`; the next different total gets rank 12. Tied rows are ordered by `emp_code`.

4. **Headcount.** The summary starts from employees who joined by month-end, then looks up their logs. Eligible employees with no logs still count; employees who join later do not.

5. **100x data.** I would use execution stats to locate expensive stages, then consider pre-aggregated daily summaries with a refresh strategy. Raw attendance and audit history would remain the source of truth so corrections stay traceable.
