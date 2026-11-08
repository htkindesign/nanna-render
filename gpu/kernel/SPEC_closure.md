<!-- SPDX-License-Identifier: GPL-3.0-or-later | Copyright 2026 Kiet Ho -->
# DwaBase closure on GPU

One page. Lobe formulas live in `lobes.h`, measurements in `out\test_lobes.txt`, `out\test_mean.txt`, `out\gpu_lobes.txt`.
"Measured" means run here. "Proposed" means designed, not built.

## Data layout (proposed)

`lobes.h` today is the test form: one `LayeredLobe` per lobe, 1012 bytes measured (`Lobe` 316, `UnderLayer` 172, 4 slots), so 7 lobes cost 7084 bytes.
MoonRay's attenuators are shared: every lobe sits under a prefix of one list, in order. So store the list once.

| Record | Bytes | Holds |
|---|---|---|
| `ClosureHeader` | 32 | lobe count, attenuator count, thin, overflow, emission |
| `CompactFresnel` | 48 | union of dielectric and conductor, weight, type, wrap, one-minus data. Velvet uses roughness only |
| `CompactLobe` | 144 | kind, `attenCount`, N, scale, albedo or tint, roughness pair (or a, b or velvet norm), etaI, etaT, favg, favgInv, abbe, one `CompactFresnel`. T is added for aniso by a kind-only trailer |
| `CompactAtten` | 88 | kind, N, thickness, attenuation color and weight, neta, one `CompactFresnel`. The exit Fresnel is rebuilt from neta |
| `IridescenceParams` | 64 | at most one per closure, referenced by a flag on the lobe |

Limits: `NK_MAX_LOBES` 8 (MoonRay 16), `NK_MAX_LAYERS` 4 (MoonRay 16). `DwaBase` without toon or hair needs 3 attenuators: fuzz, outer specular, specular. Its reachable lobes, without toon, hair or glitter: fuzz, outer specular, metallic, dielectric R, dielectric T, diffuse R, diffuse T, 7 in all.
Overflow never writes past an array: `layered_push` returns false and sets `overflow` (measured), `layered_count` clamps a bad count (measured). A dropped layer makes the lobe too bright, so the kernel must count overflows and report them.
Tables (`LobeTables`) are device buffers, 78316 floats or about 313 kB, shared by all closures. Fresnel takes the tables as an argument, because `OneMinusVelvetFresnel` reads one. `Fresnel` must not hold a pointer: cases cross from host to device.

## Lobe stack and how layers are evaluated

Order is creation order, and a lobe's `attenCount` is the attenuator count when it was added. `layers[0]` is outermost, as `BsdfBuilder::placeUnderPreviousLobes` wraps.
1. Walk attenuators 0 to `attenCount-1`, outermost first. Plain: multiply `Fresnel(cosWo) * T(cosWo, cosWi)`. Clearcoat: refract wo and wi, stop black on TIR, multiply the factor, Jacobian into the pdf. Clearcoat transmission: refract wo only, no Jacobian.
2. Evaluate the base lobe with the bent directions. Iridescence wraps the base, inside the attenuators.
3. Sampling is the same walk in two passes: wo down, base `sample`, then wi back up. `passThroughTIR` keeps wi unbent, so `sample` and `eval` disagree there on purpose.

The wo part of the walk is the same for all lobes: cache `woPrime[i]` and `cosWo[i]` per closure. The inter-reflection term needs the albedo of what lies below, which is a further walk bounded by 4.
Keep these MoonRay quirks, they set the image: BTDF and aniso are always Beckmann (MoonRay has no anisotropic GGX), BTDF energy compensation is not in its pdf, `sEpsilon` is 1e-6 and not float epsilon. Velvet indices must be written without `x * n - 1`, nvcc fuses that into an FMA and reads another row (found by the GPU check).

## Memory per path (proposed, estimate)

The closure need not outlive one shading step: MoonRay computes light contributions before the shadow test, so a shadow ray carries a finished color.

| State | Bytes |
|---|---|
| Path: ray 32, pixel 4, throughput 12, selection scale 4, rng 8, depths and flags 8, ray differential 24 | 92 |
| Shadow rays, 2 per vertex (one light, one bsdf that hit a light), ray 32 + color 12 + light id 4 | 96 |
| Per path, kept | about 190 |
| Closure while shading, typical (2 lobes, 1 attenuator) | 408 |
| Closure while shading, worst (8 lobes, 4 attenuators, iridescence) | 1688 |

2 M live paths keep about 380 MB. The closure lives on the stack or in one shade-queue slot, not per path.

## The sampling rule

For lobe k and light l, r is the pdf the lobe reports and q is the light's own pdf, without selection. `w(a,b) = a*a/(a*a+b*b)`.
Mean on one lobe: `integral f Li ( w_l + (p/r) w_b ) dw`, with p the true density of `sample()`. If `p = r`, any mix gives the integral. If not, the weights set the look.

1. Bsdf ray: `f/r`, using the reported r, never a true density.
2. Weight each lobe alone: `w(ni r, nl q)`. Never a pdf summed over lobes.
3. `ni`, `nl` are MoonRay's counts, not the GPU's: `bsdf_samples^2`, `light_samples^2` at `nonMirrorDepth` 0, and 1, 1 after. Hold them even at one sample per hit.
4. q comes from MoonRay's light: same formula, and `sample_upper_hemisphere_only` changes it. If the GPU samples a light another way, divide by the real density but keep MoonRay's q in the weight.
5. Divide by lobe-pick and light-pick probabilities last, after the weight and after the clamp.
6. Indirect: `f/r`, no weight, no clamp.
7. Clamp each lobe's direct term per sample at depth >= `sample_clamping_depth`. Value is `sample_clamping_value / bsdf_samples^2` against MoonRay's split throughput, so an unsplit GPU path compares to the value itself. The test covers depth 1 with throughput 1 only.
8. Mirrors: no MIS. `eval` is 0, so light samples skip them.

Lobes where p is not r: Oren-Nayar and FlatDiffuse with roughness above 0 or flatness, anything under a refracting clearcoat. Small, from energy compensation fits: GGX with EC, BTDF, about 0.1%. Clean: Lambert, FlatDiffuse r 0, plain GGX, velvet.

## Instruction list

One `DWABASE` instruction carries the parameter struct. Inside it, in `DwaBaseLayerable.ispc` order, with PHYSICAL behavior (under and over) tracked by `currentTransmittance` and `weightAccum`:

| Op | Lobes | State |
|---|---|---|
| `BEGIN` (thin, prevent culling), `EMISSION` | none | ported: no |
| `VELVET` (fuzz) | velvet + velvet attenuator | ported |
| `IRIDESCENCE` | block on the next lobe | hue ported, ramp left |
| `CLEARCOAT_MIRROR`, `CLEARCOAT_MICROFACET` | R + clearcoat attenuator | lobes ported |
| `GLITTER` | flakes | left |
| `ADJACENT_BEGIN`, `ADJACENT_END` | none | not ported |
| `FABRIC` | warp, weft | left |
| `TOON_SPECULAR` | toon specular lobes | left |
| `CONDUCTOR_MIRROR`, `CONDUCTOR_ISO`, `CONDUCTOR_ANISO` | R, no attenuator | lobes ported |
| `DIELECTRIC_MIRROR`, `DIELECTRIC_ISO`, `DIELECTRIC_ANISO` | R + T, simple attenuator | lobes ported, aniso T is iso with mean roughness |
| `TOON_DIFFUSE` | ramp | left |
| `FLAT_DIFFUSE`, `DIFFUSE_TRANSMISSION` | diffuse R, T | lobes ported |
| `BSSRDF` (normalized, dipole, random walk) | subsurface | left |
| `HAIR` | R, TT, TRT, TRRT, diffuse | left |

## What is left

Lobes: iridescence ramp mode, Fabric, Toon, glitter flakes, hair, bssrdf. Builder: none of `createLobes`, `ShaderIor_init`, `Intersection_adaptNormal`, weight accumulation or adjacency is ported. `CompactLobe` is unbuilt. The rule is measured on one vertex, not on a whole path, and not yet against a MoonRay render.
