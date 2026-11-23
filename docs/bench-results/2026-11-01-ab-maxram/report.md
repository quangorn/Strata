# A/B: release 0.1.55 vs fork 0.1.55-local, preset IQ2_XS max_ram

Interleaved design: 10 full standard-v1 runs per arm, order ABBA per pair (pair 1 = orig, fork; pair 2 = fork, orig; ...). Every run starts its own engine and pays the same cold model load.

## decode, short

| arm | median | mean | MAD | IQR | std | CV % | min-max | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| release 0.1.55 | 140.0 | 138.0 | 2.47 | 7.52 | 4.72 | 3.42 | 130.36-145.85 | 10 |
| fork 0.1.55-local | 139.62 | 138.79 | 2.29 | 6.11 | 5.58 | 4.02 | 129.01-147.04 | 10 |

- разница медиан (orig - fork): **+0.37 tok/s** (+0.3 %), бутстрэп 95 % CI [-7.08; +4.04], p~0.752
- Mann-Whitney U = 44.0, p = 0.6775
- парно (внутри ABBA-пар, orig - fork): медиана -0.12, среднее -0.79, orig быстрее в 5/10 пар
- знаковый тест по парам: p = 1.000

## decode, prompt-4k

| arm | median | mean | MAD | IQR | std | CV % | min-max | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| release 0.1.55 | 129.12 | 130.51 | 3.61 | 10.4 | 5.76 | 4.41 | 122.43-140.35 | 10 |
| fork 0.1.55-local | 136.93 | 136.42 | 4.73 | 8.95 | 5.24 | 3.84 | 128.93-144.04 | 10 |

- разница медиан (orig - fork): **-7.81 tok/s** (-5.7 %), бутстрэп 95 % CI [-13.05; +1.12], p~0.078
- Mann-Whitney U = 20.0, p = 0.0257
- парно (внутри ABBA-пар, orig - fork): медиана -4.78, среднее -5.91, orig быстрее в 2/10 пар
- знаковый тест по парам: p = 0.109

## decode, prompt-16k

| arm | median | mean | MAD | IQR | std | CV % | min-max | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| release 0.1.55 | 133.12 | 131.94 | 5.24 | 13.59 | 9.01 | 6.83 | 117.19-145.26 | 10 |
| fork 0.1.55-local | 130.54 | 131.56 | 4.81 | 10.21 | 6.22 | 4.73 | 125.01-144.25 | 10 |

- разница медиан (orig - fork): **+2.57 tok/s** (+2.0 %), бутстрэп 95 % CI [-7.07; +9.26], p~0.567
- Mann-Whitney U = 44.0, p = 0.6776
- парно (внутри ABBA-пар, orig - fork): медиана +1.64, среднее +0.39, orig быстрее в 5/10 пар
- знаковый тест по парам: p = 1.000

## controls (prefill must not differ)

| arm | prompt-4k tok/s | prompt-16k tok/s | n |
| --- | ---: | ---: | ---: |
| release 0.1.55 | 2081.5 | 2848.4 | 10 |
| fork 0.1.55-local | 2076.4 | 2850.3 | 10 |

## robustness: 4k decode split by how many requests hit the 512-token cap

| arm | capped | n | decode runs | median |
| --- | ---: | ---: | --- | ---: |
| release 0.1.55 | 2/3 | 6 | 125.1, 125.9, 128.9, 135.5, 137.9, 140.3 | 132.2 |
| release 0.1.55 | 3/3 | 4 | 122.4, 129.1, 129.2, 130.7 | 129.1 |
| fork 0.1.55-local | 1/3 | 1 | 139.9 | 139.9 |
| fork 0.1.55-local | 2/3 | 5 | 132.0, 132.0, 139.5, 140.5, 144.0 | 139.5 |
| fork 0.1.55-local | 3/3 | 4 | 128.9, 131.4, 134.3, 141.5 | 132.9 |

## power

- short: pooled spread 5.2 tok/s; ten runs per arm resolve about 6.5 tok/s (4.6 %) at 80 % power; anything smaller is under this design's floor.
- prompt-4k: pooled spread 5.5 tok/s; ten runs per arm resolve about 6.9 tok/s (5.0 %) at 80 % power; anything smaller is under this design's floor.
- prompt-16k: pooled spread 7.7 tok/s; ten runs per arm resolve about 9.7 tok/s (7.4 %) at 80 % power; anything smaller is under this design's floor.

`--skip-model-hash` was passed, so every run reports `nonconforming` with the single reason `no model file could be hashed`; timings are unaffected.
