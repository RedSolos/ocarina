// The present: the frame drawn into the drawable, cropped (nvn_mtl.m present_pipeline)
#include <metal_stdlib>
using namespace metal;
struct V { float4 p [[position]]; float2 uv; };
vertex V vs(uint id [[vertex_id]]) { float2 uv = float2((id << 1) & 2, id & 2); V o; o.p = float4(uv * float2(2, -2) + float2(-1, 1), 0, 1); o.uv = uv; return o; }
struct Crop { float4 rect; float4 edge; };
fragment float4 fs(V in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]], constant Crop& c [[buffer(0)]]) { return t.sample(s, clamp(in.uv * c.rect.zw + c.rect.xy, c.edge.xy, c.edge.zw)); }
// Frame generation (DM_FRAMEGEN, nvn_mtl.m fg_present_blend): the plain blend of the previous frame and the current one, shown on the ticks
// between them; mixw.x is the share of the current frame (1/2 for one generated tick, 1/3 and 2/3 for two, 1/4, 1/2 and 3/4 for three).
// Stage 1 proves the pacing and the accounting; it is not yet a picture worth judging
struct CropMix { float4 rect; float4 edge; float4 mixw; };
fragment float4 fs_blend(V in [[stage_in]], texture2d<float> t0 [[texture(0)]], texture2d<float> t1 [[texture(1)]], sampler s [[sampler(0)]], constant CropMix& c [[buffer(0)]]) { float2 uv = clamp(in.uv * c.rect.zw + c.rect.xy, c.edge.xy, c.edge.zw); return mix(t0.sample(s, uv), t1.sample(s, uv), c.mixw.x); }
