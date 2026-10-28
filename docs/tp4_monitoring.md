# TP4 monitoring and sell-quote evidence

Baseline inspected: main c51ef19977b1f13b65cc4aa6bcbbc2692e1976f6, Shadow snapshot 2026-10-28T19:48:16Z. TP4 journal exists with zero entries, zero closes and zero open positions. First-trade verification must wait for a real forward signal. Entry/exit parameters remain fixed.

Liquidity provenance audit: 85 stored BUY quotes lack per-quote tokenOut binding and block/time; 69 use fallback_fee. They do not establish token-specific sellability. The audit flags them without rewriting historical data or main PAPER accounting.

The scanner runs a bounded, read-only audit after PAPER exits and Shadow processing. It uses only public eth_blockNumber, eth_getBlockByNumber and eth_call, pins calls to one fresh block, decodes a token-bound historical eight-word exactInputSingle template, reads decimals, holder balance and router allowance, and simulates sale of the full 50-SDA model position quantity. It uses no private key, signing or broadcast method. A missing holder/allowance, route, return shape, stale input or RPC timeout is UNKNOWN. A successful simulation is not a fill, guaranteed exit or proof of executable strategy parity. WSDA output is treated as a 1:1 wrapped SDA model; no native unwrap is simulated. This is an additional diagnostic, not a replacement P/L model.

Only scanner checkpoints own strategy_lab/execution_quote_audit.json. The optimizer reads it and publishes its timestamp and trial review in strategy_evidence. Existing journal records are never reset or backfilled. New TP4 positions freeze entry evidence; closes retain observation time, peak and maximum quote gap over the entire holding interval. A gap during HOLD cannot be erased by a later fresh exit.

Milestones are 15 valid closes (preliminary), 30 (breadth), and 50 (decision review). Fixed research-quality checks require at least six tokens, at least 90% valid-quote coverage, positive net after modeled costs and invalid-close full-stake stress, profit factor >=1.15, max drawdown <=50 SDA, no single-token majority of positive token profit and no journal errors. These checks never authorize automatic promotion. Invalid and open-position risk remains separately visible.

Validation: 296 local unittest checks passed, including 11 new checks for read-only route encoding, same-block calls, full-size quantities, unknown allowance, prohibited RPC methods, fixed milestones, accounting errors, profit concentration, duplicates and gap propagation. Scanner shell syntax checked.
