# V31 exit replay — preliminary findings

Read-only observed-tick exit replay v2 available as conversation artifact. Tests hypothetical stop levels with explicit 1/2/3% transaction-cost sensitivity; excludes partial exits, records missing samples, never treats observed prices as guaranteed fills. See analysis from 2026-10-09 uploaded positions (5).json.

At assumed 2% costs, V30.2 52 replayable trades: stop -1% delta -9.99 SDA, -2% +7.89 SDA. V30.3 43 replayable trades: -1% +8.02 SDA, -2% +4.01 SDA. These are observed-price marks only, NOT realized fills or out-of-sample performance. 47 V30.2 trades lacked usable price samples. Do not deploy.
