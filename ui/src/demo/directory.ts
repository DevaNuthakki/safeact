// Display names for the local demo. The API returns people as principal
// ids and has no endpoint that resolves someone else's name, so the demo
// bootstrap (deploy/demo/bootstrap.py, DEMO_ACCOUNTS) gives its accounts
// fixed ids and this directory maps them to names. A real deployment
// would resolve names through a tenant-scoped API instead. Display only:
// nothing here grants or checks anything.

export interface Person { name: string; title: string }

export const PEOPLE: Record<string, Person> = {
  "5afe0000-0000-4000-8000-000000000101": { name: "Alex Rivera", title: "Support agent" },
  "5afe0000-0000-4000-8000-000000000102": { name: "Sam Chen", title: "Investigator" },
  "5afe0000-0000-4000-8000-000000000103": { name: "Jordan Lee", title: "Reviewer" },
  "5afe0000-0000-4000-8000-000000000104": { name: "Priya Patel", title: "Reviewer" },
  "5afe0000-0000-4000-8000-000000000105": { name: "Support assistant", title: "AI agent" },
  "5afe0000-0000-4000-8000-000000000201": { name: "Morgan Diaz", title: "Support agent" },
  "5afe0000-0000-4000-8000-000000000202": { name: "Taylor Brooks", title: "Investigator" },
  "5afe0000-0000-4000-8000-000000000203": { name: "Casey Nguyen", title: "Reviewer" },
  "5afe0000-0000-4000-8000-000000000205": { name: "Support assistant", title: "AI agent" },
};

export const COMPANIES: Record<string, string> = {
  "tenant-north": "Northwind (demo)",
  "tenant-south": "Southwind (demo)",
};
