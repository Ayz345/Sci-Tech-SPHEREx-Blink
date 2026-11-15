// SPHEREx Blink: one three.js scene behind the whole page.
//   pages    nine pages, paper and black space in turn, each saying what we built or did, pinned to the screen: the words appear in place, hold while
//            you scroll a little, and leave quickly. One form stays behind them and takes a different shape on each
//            page (flower, pair, dish with running rings, pebble, twisted column, the Earth, five petals). Black comes
//            in from the corners, no cyan (cyan is for the telescope entry only). Scrolling never plays the sequence.
//   Begin    (first and last page) the form relaxes and opens from the top like a bud into the three shield cones;
//            the rest of the model (assets/telescope.glb, shades of white) grows out of the cone foot; the dive on one
//            camera path through the interior (ribs, seams, glowing rims, barrel with rings); the mirror lights up; white flash
//   sky      the white field stop opens onto the sky, look out through the telescope
//   plate    a click anywhere on the sky (or a target, or typed RA Dec, or ?target= / ?ra=&dec=) -> zoom into
//            the field and the plate screen on real data (plate.js): gallery files (data/) for the five challenge targets,
//            the live server (config.js) for any other spot
// The craft is an illustration. The sky stars are decorative, not catalogue data, and fade before a frame shows.
// URL keys: ?page=N (start at page 0-8), ?p=0.5 (freeze the Begin sequence), ?stage=sky, ?target=<key>, ?ra=&dec=,
//           ?fast=1 (no waits in the plate screen), ?speed=2 (a faster Begin sequence).
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { ShaderPass } from "three/addons/postprocessing/ShaderPass.js";
import { Pass, FullScreenQuad } from "three/addons/postprocessing/Pass.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { createTool, loadTarget, loadBand, paint, drawMarker, playable, usedOf, dateOf, fmtDate, setUpper, findServer } from "./plate.js";

const $ = (id) => document.getElementById(id);
const P = new URLSearchParams(location.search);
const FAST = P.has("fast");
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const D2R = Math.PI / 180;
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const lerp = (a, b, t) => a + (b - a) * t;
const sstep = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const lin = (a, b, x) => clamp((x - a) / (b - a), 0, 1);
const E = { inOut: (t) => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2), in: (t) => t * t * t, out: (t) => 1 - Math.pow(1 - t, 3), sine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
  expoInOut: (t) => (t <= 0 ? 0 : t >= 1 ? 1 : t < .5 ? Math.pow(2, 20 * t - 10) / 2 : (2 - Math.pow(2, -20 * t + 10)) / 2) };
// time-based moves (the zoom into a field): driven by the render loop
const tweens = new Set();
function tween(ms, fn, ease = E.inOut) {
  return new Promise((res) => { if (ms <= 0 || REDUCED) { fn(1, 1); res(); } else tweens.add({ t0: performance.now(), ms, fn, ease, res }); });
}
const radec = (ra, dec) => new THREE.Vector3(Math.cos(dec * D2R) * Math.cos(ra * D2R), Math.sin(dec * D2R), -Math.cos(dec * D2R) * Math.sin(ra * D2R));
const toRaDec = (v) => { const d = Math.asin(clamp(v.y, -1, 1)) / D2R; let r = Math.atan2(-v.z, v.x) / D2R; if (r < 0) r += 360; return [r, d]; };
function slerpV(a, b, t) {
  const d = clamp(a.dot(b), -1, 1), th = Math.acos(d);
  if (th < 1e-5) return b.clone();
  const s = Math.sin(th);
  return a.clone().multiplyScalar(Math.sin((1 - t) * th) / s).add(b.clone().multiplyScalar(Math.sin(t * th) / s)).normalize();
}
// a value along the timeline: cubic Hermite through [p, value, slope?] keys (missing slopes: monotone, so no overshoot)
function track(keys) {
  const n = keys.length, d = [];
  for (let i = 0; i < n - 1; i++) d.push((keys[i + 1][1] - keys[i][1]) / (keys[i + 1][0] - keys[i][0]));
  const m = keys.map((k, i) => {
    if (k[2] !== undefined) return k[2];
    if (i === 0) return d[0];
    if (i === n - 1) return d[n - 2];
    return d[i - 1] * d[i] <= 0 ? 0 : (2 * d[i - 1] * d[i]) / (d[i - 1] + d[i]);
  });
  return (p) => {
    if (p <= keys[0][0]) return keys[0][1];
    if (p >= keys[n - 1][0]) return keys[n - 1][1];
    let i = 0;
    while (p > keys[i + 1][0]) i++;
    const h = keys[i + 1][0] - keys[i][0], t = (p - keys[i][0]) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * keys[i][1] + (t3 - 2 * t2 + t) * h * m[i] + (-2 * t3 + 3 * t2) * keys[i + 1][1] + (t3 - t2) * h * m[i + 1];
  };
}
let seed = 20250311;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

// ---------------------------------------------------------------- renderer (ACES tone mapping, bloom, grain)
const cv = document.getElementById("gl");
const W = () => cv.clientWidth || innerWidth, H = () => cv.clientHeight || innerHeight;
const renderer = new THREE.WebGLRenderer({ canvas: $("gl"), antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.75));
renderer.setSize(W(), H(), false);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
const scene = new THREE.Scene();
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), .04).texture;
const camera = new THREE.PerspectiveCamera(35, W() / H(), .01, 3000);

// ---------------------------------------------------------------- GLSL noise
const NOISE = /* glsl */`
vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;} vec4 mod289(vec4 x){return x-floor(x*(1./289.))*289.;}
vec4 permute(vec4 x){return mod289(((x*34.)+1.)*x);} vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1./6.,1./3.); const vec4 D=vec4(0.,.5,1.,2.);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.,i1.z,i2.z,1.))+i.y+vec4(0.,i1.y,i2.y,1.))+i.x+vec4(0.,i1.x,i2.x,1.));
  float n_=.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.+1.; vec4 s1=floor(b1)*2.+1.; vec4 sh=-step(h,vec4(0.));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.); m=m*m;
  return 42.*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

// ---------------------------------------------------------------- the page behind the scene: paper -> light blue -> deep blue
// Drawn after the bloom and only where the scene left the depth buffer empty, so the white page never blooms over
// the craft. Its colours are written as they should appear on screen (sRGB) and pushed back through the inverse of
// three's ACES curve, so tone mapping and the exposure changes of the dive do not shift them.
const glslHex = (hex) => `vec3(${[(hex >> 16) & 255, (hex >> 8) & 255, hex & 255].map((x) => (x / 255).toFixed(4)).join(",")})`;
// not a blue wash: what comes in is black space; its gradient is cyan at the corners.
const SKIN = { paper: 0xfbfbf9, c1: 0xe4f9fc, c2: 0x8fe4f3, c3: 0x22bcd8, corner: 0x1291ab, black: 0x07090b, page: 0x060607, spaceC: 0x0e1317, spaceE: 0x090c10 };
const BGU = { uDark: { value: 0 }, uSpace: { value: 0 }, uCyan: { value: 0 }, uAspect: { value: W() / H() }, uTime: { value: 0 }, uExp: { value: 1.05 },
  uInIn: { value: new THREE.Matrix3().set(.59719, .35458, .04823, .076, .90834, .01566, .0284, .13383, .83777).invert() },
  uOutIn: { value: new THREE.Matrix3().set(1.60475, -.53108, -.07367, -.10208, 1.10813, -.00605, -.00327, -.07276, 1.07602).invert() } };
const bgMat = new THREE.ShaderMaterial({
  uniforms: BGU, depthTest: true, depthWrite: false,
  blending: THREE.CustomBlending, blendEquation: THREE.AddEquation, blendSrc: THREE.OneFactor, blendDst: THREE.OneFactor,
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 1., 1.); }`,
  fragmentShader: `${NOISE}
    uniform float uDark, uSpace, uCyan, uAspect, uTime, uExp; uniform mat3 uInIn, uOutIn; varying vec2 vUv;
    vec3 s2l(vec3 c){ return mix(c / 12.92, pow((c + .055) / 1.055, vec3(2.4)), step(vec3(.04045), c)); }
    vec3 invFit(vec3 y){
      y = clamp(y, 0., .992);
      vec3 A = 1. - .983729 * y, B = .0245786 - .432951 * y, C = -(.000090537 + .238081 * y);
      return (-B + sqrt(max(B * B - 4. * A * C, 0.))) / (2. * A);
    }
    void main(){
      vec2 q = (vUv - .5) * vec2(uAspect, 1.);
      float dc = length(q) / length(vec2(uAspect, 1.) * .5);
      float wob = .07 * snoise(vec3(q * 1.1, uTime * .05)) * smoothstep(0., .15, uDark);
      // a wide, soft front: the corners go first, the centre last
      const float w = .85;
      float a = clamp((uDark * (1. + w) - (1. - dc - wob)) / w, 0., 1.);
      // paper -> pale cyan -> cyan on the moving edge, then black space; the corners keep a cyan glow
      vec3 c = mix(${glslHex(SKIN.paper)}, ${glslHex(SKIN.c1)}, smoothstep(0., .12, a));
      c = mix(c, ${glslHex(SKIN.c2)}, smoothstep(.08, .24, a));
      c = mix(c, ${glslHex(SKIN.c3)}, smoothstep(.2, .36, a));
      float kc = smoothstep(.22, 1.02, abs(vUv.x - .5) * abs(vUv.y - .5) * 4. + .5 * wob);   // 1 in the four corners, 0 along the middles of the edges
      c = mix(c, mix(${glslHex(SKIN.black)}, ${glslHex(SKIN.corner)}, kc * kc), smoothstep(.26, .56, a));
      // the pages: paper straight to black, the black coming in from the corners (no cyan here)
      c = mix(mix(${glslHex(SKIN.paper)}, ${glslHex(SKIN.page)}, smoothstep(.04, .8, a)), c, uCyan);
      // the sky is deeper, with a faint glow from the top
      vec2 pp = vUv - vec2(.5, 1.12); pp.x *= uAspect * .55;
      c = mix(c, mix(${glslHex(SKIN.spaceE)}, ${glslHex(SKIN.spaceC)}, exp(-dot(pp, pp) * 2.4)), uSpace);
      gl_FragColor = vec4(uInIn * invFit(uOutIn * s2l(c)) * (.6 / uExp), 1.);
    }`,
});
class BackdropPass extends Pass {
  constructor(material) { super(); this.needsSwap = false; this.quad = new FullScreenQuad(material); }
  render(renderer, writeBuffer, readBuffer) {
    const auto = renderer.autoClear;
    renderer.autoClear = false;
    renderer.setRenderTarget(this.renderToScreen ? null : readBuffer);
    this.quad.render(renderer);
    renderer.autoClear = auto;
  }
}
const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(W(), H()), 0, .5, .9);
composer.addPass(bloom);
composer.addPass(new BackdropPass(bgMat));
composer.addPass(new OutputPass());
const grain = new ShaderPass({
  uniforms: { tDiffuse: { value: null }, uTime: { value: 0 }, uAmt: { value: 0 }, uVig: { value: 0 }, uRes: { value: new THREE.Vector2(W(), H()) } },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: `uniform sampler2D tDiffuse; uniform float uTime, uAmt, uVig; uniform vec2 uRes; varying vec2 vUv;
    float h(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
    void main(){
      vec4 c = texture2D(tDiffuse, vUv);
      float n = h(floor(vUv * uRes) + fract(uTime * 7.13) * 91.7) - .5;
      vec2 q = vUv - .5; q.x *= uRes.x / uRes.y;
      float vig = smoothstep(1.15, .25, length(q));
      c.rgb = c.rgb * mix(1., mix(.6, 1., vig), uVig) + n * uAmt;
      gl_FragColor = c;
    }`,
});
composer.addPass(grain);

// view centre: where the optical axis lands on screen (the sky sits a little above centre)
const view = { cx: .5, cy: .5 };
const S = { p: 0, goal: 0, spin: 0, time: 0, last: performance.now(), fov0: 35, mode: P.has("p") || P.get("stage") === "sky" || P.has("target") || P.has("ra") ? "frozen" : "home",
  sky: { dir: radec(281, -4), goal: radec(281, -4), fov: 50 }, live: false, drag: false };
function applyView() {
  const w = W(), h = H();
  camera.aspect = w / h;
  camera.setViewOffset(w, h, w / 2 - view.cx * w, h / 2 - view.cy * h, w, h);
  camera.updateProjectionMatrix();
}

// ---------------------------------------------------------------- sky: procedural decorative stars, Milky Way weighted, RA/Dec grid
const R_SKY = 500;
const NGP = radec(192.859, 27.128), GC = radec(266.405, -28.936);
function skyPoint() { const z = rnd() * 2 - 1, ph = rnd() * Math.PI * 2, r = Math.sqrt(1 - z * z); return new THREE.Vector3(r * Math.cos(ph), z, r * Math.sin(ph)); }
const starU = { uTime: { value: 0 }, uPx: { value: renderer.getPixelRatio() }, uFade: { value: 0 } };
function makeStars(n, weight, lumFn, sizeK, alphaK) {
  const pos = new Float32Array(n * 3), lum = new Float32Array(n), sd = new Float32Array(n);
  let i = 0;
  while (i < n) {
    const v = skyPoint();
    if (rnd() > weight(v)) continue;
    pos[i * 3] = v.x * R_SKY; pos[i * 3 + 1] = v.y * R_SKY; pos[i * 3 + 2] = v.z * R_SKY;
    lum[i] = lumFn(); sd[i] = rnd(); i++;
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("lum", new THREE.BufferAttribute(lum, 1));
  g.setAttribute("sd", new THREE.BufferAttribute(sd, 1));
  const m = new THREE.ShaderMaterial({
    uniforms: { ...starU, uSize: { value: sizeK }, uAlpha: { value: alphaK } },
    vertexShader: `uniform float uTime, uPx, uFade, uSize, uAlpha; attribute float lum; attribute float sd; varying float vA;
      void main(){
        float tw = .78 + .22 * sin(uTime * (.6 + sd * 2.4) + sd * 61.);
        vA = (.16 + .84 * lum) * tw * .65 * uFade * uAlpha;
        vec4 mv = modelViewMatrix * vec4(position, 1.);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = (1. + 3.4 * lum * lum) * uSize * uPx;
      }`,
    fragmentShader: `varying float vA;
      void main(){ float r = length(gl_PointCoord - .5); float a = smoothstep(.5, .0, r); gl_FragColor = vec4(vec3(1.), a * a * vA); }`,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  });
  const p = new THREE.Points(g, m);
  p.frustumCulled = false; p.renderOrder = -5;
  return p;
}
const galW = (v) => { const sb = v.dot(NGP), bul = Math.exp(-Math.pow(Math.acos(clamp(v.dot(GC), -1, 1)) / .5, 2)); return clamp(.2 + .7 * Math.exp(-sb * sb / .025) + .5 * bul, 0, 1); };
const hazeW = (v) => {
  const sb = v.dot(NGP), bul = Math.exp(-Math.pow(Math.acos(clamp(v.dot(GC), -1, 1)) / .42, 2));
  const lane = .55 + .45 * Math.sin(v.x * 23 + v.z * 17) * Math.sin(v.y * 31 - v.x * 11 + v.z * 7);
  return clamp(Math.exp(-sb * sb / .004) * (.35 + 1.4 * bul) * lane, 0, 1);
};
const skyG = new THREE.Group();
skyG.add(makeStars(42000, galW, () => Math.pow(rnd(), 4.6), 1.1, 1), makeStars(60000, hazeW, () => .2 + .2 * rnd(), 4.2, .04));
const gridMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false });
{
  const pts = [], Rg = R_SKY * .96;
  for (let ra = 0; ra < 360; ra += 30) for (let d = -80; d < 80; d += 2) pts.push(radec(ra, d).multiplyScalar(Rg), radec(ra, d + 2).multiplyScalar(Rg));
  for (let d = -60; d <= 60; d += 30) for (let ra = 0; ra < 360; ra += 2) pts.push(radec(ra, d).multiplyScalar(Rg), radec(ra + 2, d).multiplyScalar(Rg));
  const grid = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), gridMat);
  grid.frustumCulled = false; skyG.add(grid);
}
scene.add(skyG);

// ---------------------------------------------------------------- SPHEREx: the model (assets/telescope.glb)
// The GLB is placed at scale .9, lifted down by 1.35 (its cones run y 1.14..3.22 in model units). Everything it
// shows from outside is recoloured to shades of white (no coloured parts). Its three cones are not
// drawn: the clay layers end as exactly those cones and stay. Its hood is replaced by the barrel below.
const GS = .9, GY = -1.35;
const gltf = await new GLTFLoader().loadAsync("assets/telescope.glb");
const rig = gltf.scene.getObjectByName("RIG_turntable");
const craft = new THREE.Group();
scene.add(craft);
const model = new THREE.Group();
model.scale.setScalar(GS); model.position.y = GY;
model.add(rig);
craft.add(model);
const TONES = {
  MAT_hull_white: { color: 0xecebe6, metalness: 0, roughness: .5 },
  MAT_metal_silver: { color: 0xdadce0, metalness: .4, roughness: .36 },
  MAT_dark_black: { color: 0xc3c5c9, metalness: .1, roughness: .6 },       // boxes, ports, harness: light grey
  MAT_gold: { color: 0xf3f1ea, metalness: .3, roughness: .32 },            // trackers, hinge pins: pearl
  MAT_panel_black: { color: 0xcfd2d7, metalness: .45, roughness: .26 },    // the solar panel: pale silver
};
const toneMats = new Map();
const toneMat = (name) => {
  if (!toneMats.has(name)) toneMats.set(name, new THREE.MeshStandardMaterial({ ...(TONES[name] || TONES.MAT_metal_silver), side: THREE.DoubleSide }));
  return toneMats.get(name);
};
const insideMat = new THREE.MeshStandardMaterial({ color: 0x2a2b2e, metalness: .8, roughness: .45, side: THREE.DoubleSide });   // deck
// the dark surfaces inside the telescope keep a floor of very dark teal (about #0c181c on screen): never pure black
const FLOOR = new THREE.Color().setRGB(.014, .032, .038), floorMats = [insideMat];
const bandMat = new THREE.MeshStandardMaterial({ ...TONES.MAT_metal_silver, side: THREE.DoubleSide, transparent: true, opacity: 0 });
// a group that scales and turns about a point of the model
const pivot = (parent, x, y, z) => { const a = new THREE.Group(), b = new THREE.Group(); a.position.set(x, y, z); b.position.set(-x, -y, -z); a.add(b); parent.add(a); return { a, b }; };
const gCore = pivot(rig, 0, 1.2, 0);           // deck and optics, inside the inner cone
const gBus = pivot(rig, 0, 1.15, 0);           // bus, sensors: grow out of the foot of the cones
const gSol = pivot(gBus.b, .45, .7, -.45);     // the solar panel unfolds from its arms
const gOuter = new THREE.Group();              // the two bands ride on the outer cone
rig.add(gOuter);
for (const o of [...rig.children]) {
  const n = o.name;
  if (!n) continue;                                                                   // the groups above
  if (/^SHD_cone/.test(n) || n === "OPT_hood" || n === "SHD_brackets") { rig.remove(o); continue; }   // brackets: from inside they read as loose tabs
  const inside = /^(OPT|PLD)/.test(n);
  o.traverse((m) => { if (m.isMesh) m.material = inside ? insideMat : /^SHD_band/.test(n) ? bandMat : toneMat(m.material.name); });
  if (n === "PLD_deck") o.scale.set(.93, 1, .93);                                     // its dark edge stays inside the cone feet
  (inside ? gCore.b : /^SOL/.test(n) ? gSol.b : /^SHD/.test(n) ? gOuter : gBus.b).add(o);
}
// the barrel: an open barrel (off-axis design, no secondary in the way) with baffle rings and the mirror at
// its foot. The mirror is black and only lights up at the end of the fall.
const GLOW = new THREE.Color(0xd6f6fb);
const mirrorMat = new THREE.MeshBasicMaterial({ color: 0x000000 });
{
  const tel = new THREE.Group();
  tel.position.y = 1.22; tel.scale.setScalar(.23 / .25);       // the GLB hood: radius .23, standing on the deck at 1.22
  const flat = (mesh, y) => { mesh.rotation.x = -Math.PI / 2; mesh.position.y = y; tel.add(mesh); return mesh; };
  const wall = new THREE.Mesh(new THREE.CylinderGeometry(.25, .25, .7, 96, 1, true), new THREE.MeshStandardMaterial({ color: 0x0b0c10, metalness: .3, roughness: .85, side: THREE.BackSide }));
  floorMats.push(wall.material);
  const sleeve = new THREE.Mesh(new THREE.CylinderGeometry(.262, .262, .7, 96, 1, true), new THREE.MeshStandardMaterial({ color: 0xd6d8dc, metalness: .3, roughness: .45 }));
  wall.position.y = sleeve.position.y = .35; tel.add(wall, sleeve);
  const ringMat = new THREE.MeshStandardMaterial({ color: 0x1d1e20, metalness: .6, roughness: .5, side: THREE.DoubleSide });
  const edgeMat = new THREE.MeshBasicMaterial({ color: 0x9a9a9a });
  floorMats.push(ringMat);
  for (const y of [.14, .26, .38, .5, .62]) {
    flat(new THREE.Mesh(new THREE.RingGeometry(.185, .25, 96), ringMat), y);
    flat(new THREE.Mesh(new THREE.TorusGeometry(.185, .0035, 6, 96), edgeMat), y);
  }
  flat(new THREE.Mesh(new THREE.TorusGeometry(.256, .012, 8, 96), new THREE.MeshStandardMaterial({ color: 0xe8e8e8, metalness: 1, roughness: .2 })), .7);
  flat(new THREE.Mesh(new THREE.CircleGeometry(.249, 96), mirrorMat), .012);
  gCore.b.add(tel);
}

// The three layers. Each is one surface (u from cone foot to rim, phi around), computed in the vertex shader.
// At home all three are the same closed form at three sizes, one inside the other, so only the outer one shows:
//   radius = f(direction) only, so the form cannot fold through itself, and the petals are polynomials of the
//   direction (x^3 - 3xz^2, x^2 - z^2), so it is smooth at the poles too.
// Then every point moves straight to its place on the layer's cone (exactly the GLB cone): the rim leads, the
// feet of all three arrive together, the inner layers open their tops a little later.
const coneW = (rb, y0, rt, y1) => [rb * GS, rt * GS, y0 * GS + GY, (y1 - y0) * GS];   // GLB cone -> world
const SH = [ // cone (world); size at home; how much later its top opens; ribs; outer face; inner face (metal tones)
  { cone: coneW(.6, 1.1436, 2.06, 3.2236), k: 1, lag: 0, ribs: 64, out: [0xecebe6, 0, .5], inn: [0xd4d6d9, .16] },
  { cone: coneW(.58, 1.14, 1.62, 2.9), k: .96, lag: .09, ribs: 48, out: [0xdfe1e4, .45, .34], inn: [0xb9bcc0, .23] },
  { cone: coneW(.56, 1.18, 1.2, 2.46), k: .92, lag: .18, ribs: 40, out: [0xd8dade, .45, .34], inn: [0xa0a4a9, .3] },
];
const SHU = { uTime: { value: 0 }, uM: { value: 0 }, uCalm: { value: 0 }, uRim: { value: 0 }, uPaper: { value: new THREE.Color(5.2, 5.2, 5.1) },
  uShape: { value: new THREE.Vector4(.56, .2, 0, .12) },      // petals of order 3, 2, 4 and the noise
  uPose: { value: new THREE.Vector4(1.25, .66, 1.02, 1.78) },    // twist, flatten (y scale), tilt, size
  uShape2: { value: new THREE.Vector4(0, 0, 0, 0) },            // petals of order 5, running rings
  uEarth: { value: 0 }, uLand: { value: null }, uLandSoft: { value: null }, uLandCol: { value: new THREE.Color(0xd2d2ce) } };
// the coastlines for the Earth page: Natural Earth 1:110m land (public domain), drawn into an equirectangular mask
{
  const blank = new THREE.DataTexture(new Uint8Array([0, 0, 0, 255]), 1, 1); blank.needsUpdate = true;
  SHU.uLand.value = SHU.uLandSoft.value = blank;
  fetch("assets/land-110m.json").then((r) => r.json()).then((topo) => {
    const [sx, sy] = topo.transform.scale, [tx, ty] = topo.transform.translate;
    const arcs = topo.arcs.map((a) => { let x = 0, y = 0; return a.map(([dx, dy]) => { x += dx; y += dy; return [x * sx + tx, y * sy + ty]; }); });
    const ring = (ids) => { const pts = []; for (const i of ids) { const a = i < 0 ? arcs[~i].slice().reverse() : arcs[i]; pts.push(...(pts.length ? a.slice(1) : a)); } return pts; };
    const draw = (Wc, Hc, blur) => {
      const c = document.createElement("canvas"); c.width = Wc; c.height = Hc;
      const g = c.getContext("2d");
      g.fillStyle = "#000"; g.fillRect(0, 0, Wc, Hc);
      if (blur) g.filter = `blur(${blur}px)`;
      g.fillStyle = "#fff"; g.beginPath();
      // rings that cross the 180th meridian are unwrapped (continuous longitudes) and drawn three times, a world
      // apart, so no line runs across the map; a ring that goes all the way round (Antarctica) is drawn as it is
      for (const geo of topo.objects.land.geometries) for (const poly of geo.type === "Polygon" ? [geo.arcs] : geo.arcs) for (const r of poly) {
        const pts = ring(r), un = [pts[0].slice()];
        for (let k = 1; k < pts.length; k++) {
          let lon = pts[k][0];
          while (lon - un[k - 1][0] > 180) lon -= 360;
          while (lon - un[k - 1][0] < -180) lon += 360;
          un.push([lon, pts[k][1]]);
        }
        const round = Math.abs(un.at(-1)[0] - un[0][0]) > 300;
        for (const off of round ? [0] : [-360, 0, 360]) {
          (round ? pts : un).forEach(([lon, lat], k) => { const x = (lon + off + 180) / 360 * Wc, y = (90 - lat) / 180 * Hc; k ? g.lineTo(x, y) : g.moveTo(x, y); });
          g.closePath();
        }
      }
      g.fill("evenodd");
      const t = new THREE.CanvasTexture(c);
      t.wrapS = THREE.RepeatWrapping; t.generateMipmaps = false; t.minFilter = t.magFilter = THREE.LinearFilter;
      return t;
    };
    SHU.uLand.value = draw(2048, 1024, .8);
    SHU.uLandSoft.value = draw(1024, 512, 3);
    window.__land = SHU.uLand.value.image;
  }).catch(() => {});
}
const SHELL_GLSL = /* glsl */`
uniform float uTime, uM, uK, uCalm, uLag, uEarth; uniform vec4 uCone, uShape, uPose, uShape2; uniform sampler2D uLandSoft;
varying float vAO; varying vec3 vEDir;
${NOISE}
const vec3 BC = vec3(0., .15, 0.);
float gR = 1.;
// the land is looked up turned by a fixed .7 rad: the mesh's seam (phi = 0, z = 0) must not meet atan's zero, where the
// lookup came back wrong on SwiftShader and cut a groove along the meridian (the fragment uses the same turn)
vec2 lonlat(vec3 d){ vec2 r = mat2(.764842, .644218, -.644218, .764842) * d.xz; return vec2(atan(-r.y, r.x) / (2. * PI) + .5, asin(clamp(d.y, -1., 1.)) / PI + .5); }
vec3 rotA(vec3 p, vec3 k, float a){ return p * cos(a) + cross(k, p) * sin(a) + k * dot(k, p) * (1. - cos(a)); }
vec3 blobD(vec3 d){
  float wild = 1. - uCalm;
  // three petals that spiral slowly around the axis, a two-lobed swell across them, a little low noise
  // (the amounts come from uShape / uPose: each page of the site has its own, and they blend while scrolling)
  float t1 = uPose.x * d.y + .22 * sin(uTime * .11);
  vec2 e = mat2(cos(t1), -sin(t1), sin(t1), cos(t1)) * d.xz;
  float p3 = e.x * (e.x * e.x - 3. * e.y * e.y);
  float e2 = e.x * e.x - e.y * e.y;
  float p4 = e2 * e2 - 4. * e.x * e.x * e.y * e.y;         // four petals: x^4 - 6x^2z^2 + z^4
  float t2 = -.72 * uPose.x * d.y + 2.1 + .18 * sin(uTime * .08 + 1.3);
  vec2 f = mat2(cos(t2), -sin(t2), sin(t2), cos(t2)) * d.xz;
  float p2 = f.x * f.x - f.y * f.y;
  float p5 = e.x * (e.x * e.x * e.x * e.x - 10. * e.x * e.x * e.y * e.y + 5. * e.y * e.y * e.y * e.y);   // five petals
  // rings that run out from the axis, pass after pass
  float th = acos(clamp(d.y, -1., 1.));
  float rip = sin(th * 13. - uTime * .9) * smoothstep(.05, .5, th) * smoothstep(PI, PI - .7, th);
  float n = snoise(d * .75 + vec3(3.1, uTime * .04, 7.7 + uTime * .028));
  float g = 1. + wild * (uShape.x * p3 + uShape.y * p2 + uShape.z * p4 + uShape.w * n + uShape2.x * p5 + uShape2.y * rip) - .3;
  // the Earth: a sphere whose continents stand out a little from the sea
  if (uEarth > .001) g = mix(g, .7 + .045 * texture2D(uLandSoft, lonlat(d)).r, uEarth * wild);
  gR = .3 + .5 * (g + sqrt(g * g + .02));                 // a soft floor: the valleys never pinch shut
  vec3 p = d * mix(1.55, uPose.w, wild) * gR;
  // flattened along its own axis and tipped to face us, like a flower; it rights itself as it opens
  p.y *= mix(1., uPose.y, wild);
  p = rotA(p, normalize(vec3(1., .12, -.2)), wild * (uPose.z + .05 * sin(uTime * .07)));
  return BC + p * uK;
}
vec3 dirOf(float u, float ph){ float th = PI * u; return vec3(sin(th) * cos(ph), -cos(th), sin(th) * sin(ph)); }
vec3 coneP(float u, float ph){ float r = mix(uCone.x, uCone.y, u); return vec3(r * cos(ph), uCone.z + uCone.w * u, r * sin(ph)); }
float openK(float u){
  float uu = clamp(u, 0., 1.);
  return smoothstep(0., 1., clamp(uM * 1.43 - .25 * (1. - uu) - uLag * smoothstep(.55, 1., uu), 0., 1.));
}
vec3 shellP(float u, float ph){
  float k = openK(u);
  if (k >= 1.) return coneP(u, ph);
  vec3 b = blobD(dirOf(u, ph));
  return k <= 0. ? b : mix(b, coneP(u, ph), k);
}`;
const shells = [];
{
  const NU = 140, NPH = 220;
  const geo = (() => {
    const pos = new Float32Array((NU + 1) * (NPH + 1) * 3), uv = new Float32Array((NU + 1) * (NPH + 1) * 2), idx = [];
    let v = 0;
    for (let i = 0; i <= NU; i++) for (let j = 0; j <= NPH; j++, v++) {
      pos.set([lerp(.0008, .9992, i / NU), (j / NPH) * Math.PI * 2, 0], v * 3); uv.set([j / NPH, i / NU], v * 2);
    }
    for (let i = 0; i < NU; i++) for (let j = 0; j < NPH; j++) { const a = i * (NPH + 1) + j, b = a + NPH + 1; idx.push(a, b, a + 1, a + 1, b, b + 1); }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
    g.setIndex(idx);
    return g;
  })();
  SH.forEach((d) => {
    const own = { uK: { value: d.k }, uLag: { value: d.lag }, uCone: { value: new THREE.Vector4(...d.cone) },
      uInCol: { value: new THREE.Color(0xffffff) }, uInRough: { value: .92 }, uInMetal: { value: 0 } };
    // one material for both faces: the outer face takes the material's own values, the inner face the uIn* ones.
    // polygonOffset lets the rib and seam lines sit on the surface.
    const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, metalness: 0, roughness: .92, side: THREE.DoubleSide, envMapIntensity: .25,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    mat.onBeforeCompile = (sh) => {
      Object.assign(sh.uniforms, SHU, own);
      sh.vertexShader = sh.vertexShader
        .replace("#include <common>", "#include <common>\n" + SHELL_GLSL)
        .replace("#include <beginnormal_vertex>", `
          float sU = position.x, sPh = position.y;
          vEDir = dirOf(sU, sPh);
          vec3 sP0 = shellP(sU, sPh);
          float sK = openK(sU);
          vAO = mix(1., mix(.62, 1., smoothstep(.45, 1.25, gR)), (1. - uCalm) * (1. - sK));
          vec3 sN = cross(shellP(sU + .003, sPh) - sP0, shellP(sU, sPh + .003) - sP0);
          // while the form is closed its normal comes from the direction field: the (u, phi) grid collapses at the
          // poles and leaves a crease along its seam (phi = 0); the grid takes over once the layer starts to open
          float sW = smoothstep(0., .04, 4. * sK);
          vec3 objectNormal = normalize(sN);
          if (sW < 1.) {
            vec3 sD = dirOf(sU, sPh);
            vec3 sT1 = normalize(cross(sD, abs(sD.z) < .9 ? vec3(0., 0., 1.) : vec3(1., 0., 0.))), sT2 = cross(sD, sT1);
            vec3 sB0 = blobD(sD);
            vec3 sND = normalize(cross(blobD(normalize(sD + .006 * sT1)) - sB0, blobD(normalize(sD + .006 * sT2)) - sB0));
            objectNormal = normalize(mix(sND, objectNormal, sW));
          }`)
        .replace("#include <begin_vertex>", "vec3 transformed = sP0;");
      sh.fragmentShader = sh.fragmentShader
        .replace("#include <common>", "#include <common>\nvarying float vAO; varying vec3 vEDir; uniform vec3 uInCol, uPaper, uLandCol; uniform float uInRough, uInMetal, uRim, uEarth; uniform sampler2D uLand;")
        .replace("#include <color_fragment>", `#include <color_fragment>
          float sFk = gl_FrontFacing ? 1. : 0.;
          float sLand = 0.;
          if (uEarth > .001) { vec3 ed = normalize(vEDir); vec2 er = mat2(.764842, .644218, -.644218, .764842) * ed.xz; sLand = sFk * uEarth * texture2D(uLand, vec2(atan(-er.y, er.x) / (2. * PI) + .5, asin(clamp(ed.y, -1., 1.)) / PI + .5)).r; }
          diffuseColor.rgb = mix(mix(uInCol, diffuseColor.rgb, sFk), uLandCol, sLand) * vAO;`)
        .replace("#include <roughnessmap_fragment>", "#include <roughnessmap_fragment>\nroughnessFactor = mix(mix(uInRough, roughnessFactor, sFk), .82, sLand);")
        .replace("#include <metalnessmap_fragment>", "#include <metalnessmap_fragment>\nmetalnessFactor = mix(mix(uInMetal, metalnessFactor, sFk), 0., sLand);")
        // the clay's edge melts into the page (the reference): a soft rim of paper white
        .replace("#include <opaque_fragment>", "outgoingLight = mix(outgoingLight, uPaper, uRim * pow(1. - saturate(dot(normalize(vViewPosition), normal)), 3.4));\n#include <opaque_fragment>");
    };
    mat.customProgramCacheKey = () => "spherex-shell-6";
    mat.userData = { own, outC: new THREE.Color(d.out[0]), outM: d.out[1], outR: d.out[2], inC: new THREE.Color(d.inn[0]), inR: d.inn[1] };
    const mesh = new THREE.Mesh(geo, mat);
    mesh.frustumCulled = false;
    craft.add(mesh); shells.push(mesh); floorMats.push(mat);
  });
}
// the dressing of each cone: ribs and seams as hairlines, a glowing rim, a foot ring. They fade in once the cone stands.
const lineMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false });
const seamMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false });
const footMat = new THREE.MeshStandardMaterial({ color: 0xb9bcc0, metalness: .6, roughness: .4, transparent: true, opacity: 0 });
const rimMats = [], dress = [];
SH.forEach((d) => {
  const [rb, rt, y0, h] = d.cone, g = new THREE.Group(), rib = [];
  for (let k = 0; k < d.ribs; k++) {
    const a = (k / d.ribs) * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a);
    rib.push(new THREE.Vector3(rb * ca, y0, rb * sa), new THREE.Vector3(rt * ca, y0 + h, rt * sa));
  }
  g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(rib), lineMat));
  for (const f of [.22, .48, .74]) {
    const r = lerp(rb, rt, f), pts = [];
    for (let k = 0; k <= 160; k++) { const a = (k / 160) * Math.PI * 2; pts.push(new THREE.Vector3(r * Math.cos(a), y0 + h * f, r * Math.sin(a))); }
    g.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), seamMat));
  }
  const rimMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(1, 1, 1).multiplyScalar(1.4), transparent: true, opacity: 0, depthWrite: false });
  const rim = new THREE.Mesh(new THREE.TorusGeometry(rt, .011, 8, 220), rimMat);
  rim.rotation.x = Math.PI / 2; rim.position.y = y0 + h; g.add(rim); rimMats.push(rimMat);
  const foot = new THREE.Mesh(new THREE.TorusGeometry(rb, .013, 8, 120), footMat);
  foot.rotation.x = Math.PI / 2; foot.position.y = y0; g.add(foot);
  g.traverse((o) => (o.frustumCulled = false));
  craft.add(g); dress.push(g);
});
const sun = new THREE.DirectionalLight(0xffffff, 3.2);
const rimLight = new THREE.DirectionalLight(0xffffff, 1.1);
const hemi = new THREE.HemisphereLight(0xffffff, 0x9a9a9a, 1.6);
sun.position.set(4.5, 6, 3.5); rimLight.position.set(-5, -2.5, -4);
scene.add(sun, rimLight, hemi, new THREE.AmbientLight(0xffffff, .03));

// ---------------------------------------------------------------- the pages: pinned, and a shape each
// Scrolling does not play the telescope sequence (that is Begin). It turns the pages: paper and black space in turn.
// Each page is pinned to the screen. Its words appear in place, hold while you scroll a little (STEP screens of
// scroll per page, the words hold for HOLD of it) and leave quickly; nothing slides. Between two pages, while no words
// are up, the form changes shape and the black comes in from the corners (or leaves).
const PAGES = [...document.querySelectorAll(".page")];
const INNERS = PAGES.map((el) => [...el.querySelectorAll(":scope > .inner, :scope > .aside")]);
const STEP = 1.5, HOLD = .45, GO = .12;
const FORM = [ // tone 0 paper, 1 space · pet: petals of order 3, 2, 4, 5 · n noise · rip running rings · twist, flat (y scale),
               // tilt, size · x, y, s: place and scale beside the words · earth: 1 = the globe · tz: axial tilt (globe)
               // slot: 1 = placed in the page's empty .slot column (page 4, beside the real plate)
  { tone: 0, pet: [.56, .2, 0, 0], n: .12, rip: 0, twist: 1.25, flat: .66, tilt: 1.02, size: 1.78, x: 0, y: 0, s: 1, earth: 0, tz: 0 },          // 0 hero: the flower
  { tone: 1, pet: [0, 0, 0, 0], n: 0, rip: 0, twist: 0, flat: 1, tilt: 0, size: 1.5, x: 1.75, y: 0, s: .95, earth: 1, tz: .41 },                // 1 what we built: the Earth (anyone can look)
  { tone: 0, pet: [0, 0, .1, 0], n: .04, rip: .17, twist: .2, flat: .34, tilt: .92, size: 1.95, x: -1.55, y: 0, s: .8, earth: 0, tz: 0 },      // 2 reading the archive: a dish, rings running out
  { tone: 1, pet: [0, 0, .34, 0], n: .04, rip: 0, twist: .05, flat: .5, tilt: 1.2, size: 1.6, x: 1.75, y: 0, s: .9, earth: 0, tz: 0 },         // 3 cleaning: a soft square tile, the fixed grid
  { tone: 0, pet: [0, 0, 0, 0], n: .03, rip: 0, twist: 0, flat: .17, tilt: 1.53, size: 1.6, x: 1.9, y: 0, s: .9, earth: 0, tz: 0, slot: 1 },     // 4 what we found: the plate sits on a round lens, centred behind it
  { tone: 1, pet: [0, .58, 0, 0], n: .05, rip: 0, twist: 4.4, flat: 1.6, tilt: .22, size: 1.3, x: -1.6, y: 0, s: .74, earth: 0, tz: 0 },       // 5 the spectrum: a twisted column
  { tone: 0, pet: [0, .8, 0, 0], n: .06, rip: 0, twist: .45, flat: 1.08, tilt: .32, size: 1.42, x: 1.55, y: 0, s: .7, earth: 0, tz: 0 },       // 6 measure and report: a pair (two frames, one shift)
  { tone: 1, pet: [0, 0, 0, .62], n: .05, rip: 0, twist: .12, flat: .42, tilt: 1.3, size: 1.7, x: -1.65, y: 0, s: .82, earth: 0, tz: 0 },      // 7 five targets: five petals
  { tone: 0, pet: [.56, .2, 0, 0], n: .12, rip: 0, twist: 1.25, flat: .66, tilt: 1.02, size: 1.6, x: 1.62, y: 0, s: .66, earth: 0, tz: 0 },    // 8 limits and credits: the flower again
];
const flat = (f) => [...f.pet, f.n, f.rip, f.twist, f.flat, f.tilt, f.size, f.x, f.y, f.s, f.earth, f.tz, f.slot || 0];
const FORMV = FORM.map(flat), NF = FORMV[0].length;
const SCR0 = { f: FORMV[0], tone: 0, ink: 0, pos: 0, op0: 1, sc0: 1, bl0: 0 };
const scr = { f: FORMV[0].slice(), tone: 0, ink: 0, pos: 0, op0: 1, sc0: 1, bl0: 0 };
const INKC = new THREE.Color(0x141517), PAPER_HDR = new THREE.Color(5.2, 5.2, 5.1), EDGE_SPACE = new THREE.Color(.16, .165, .175);
const pageFit = [], pageShift = [];
// the document's scroll length, and (on small screens) a scale that fits a tall page into the screen
function measurePages() {
  $("track").style.height = ((PAGES.length - 1) * STEP + 1) * innerHeight + "px";
  INNERS.forEach((els, j) => {
    pageFit[j] = 1;
    if (!j || !els.length) return;
    const pad = parseFloat(getComputedStyle(PAGES[j]).paddingTop) || 0, prev = els.map((el) => el.style.transform);
    for (const el of els) el.style.transform = "none";
    // stacked (phone): the heights add up; side by side (page 4's words and plate on a wide screen): the taller one
    const stacked = els.length > 1 && els[1].offsetTop >= els[0].offsetTop + els[0].offsetHeight - 1;
    const h = els.length > 1 && !stacked ? Math.max(...els.map((el) => el.offsetHeight)) : els.reduce((a, el) => a + el.offsetHeight, 0) + (els.length > 1 ? 26 : 0);
    els.forEach((el, k) => (el.style.transform = prev[k]));
    pageFit[j] = Math.min(1, (innerHeight - 2 * pad + 40) / Math.max(1, h));
    // a page taller than the screen overflows its grid (downwards on a wide screen, both ways on a phone), so after the
    // scale the words would not sit in the middle: move the group's measured centre to the screen's centre
    const top = Math.min(...els.map((el) => el.offsetTop)), bot = Math.max(...els.map((el) => el.offsetTop + el.offsetHeight));
    pageShift[j] = pageFit[j] < 1 ? PAGES[j].clientHeight / 2 - (top + bot) / 2 : 0;
  });
}
// y = the (smoothed) scroll position that drives the form and the page colour; the words follow the real one
function readScroll(y) {
  const n = FORMV.length, u = clamp(y / innerHeight / STEP, 0, n - 1), i = Math.min(n - 2, Math.floor(u)), fr = u - i;
  const kf = sstep(.3, .7, fr);
  for (let j = 0; j < NF; j++) scr.f[j] = lerp(FORMV[i][j], FORMV[i + 1][j], kf);
  scr.tone = lerp(FORM[i].tone, FORM[i + 1].tone, sstep(.42, .58, fr));
  scr.ink = lerp(FORM[i].tone, FORM[i + 1].tone, sstep(.38, .62, fr));
  scr.pos = i + kf;
  const sr = scrollY / innerHeight;
  PAGES.forEach((el, j) => {
    const d = sr - j * STEP, k = sstep(HOLD, HOLD + GO, Math.abs(d)), op = 1 - k;
    const sc = 1 + Math.sign(d) * .035 * k, bl = 9 * k;                // comes in from a little smaller, leaves a little larger
    if (j === 0) { scr.op0 = op; scr.sc0 = sc; scr.bl0 = bl; return; }
    for (const e of INNERS[j]) {
      e.style.opacity = op; e.style.visibility = op < .01 ? "hidden" : "visible";
      // .slide (page 4's track chart): comes in from the right and leaves to the right
      const tx = e.classList.contains("slide") ? `translateX(${(k * Math.min(240, innerWidth * .16)).toFixed(1)}px) ` : "";
      const ty = pageShift[j] ? `translateY(${pageShift[j].toFixed(1)}px) ` : "";
      e.style.transform = `${tx}${ty}scale(${sc * pageFit[j]})`; e.style.filter = bl > .05 ? `blur(${bl.toFixed(1)}px)` : "none";
    }
  });
}

// ---------------------------------------------------------------- the timeline
const T = { form: [.05, .41], calm: [.04, .3], metal: [.22, .42], core: [.1, .24], bus: [.2, .4], panel: [.29, .45],
  bands: [.35, .43], dress: [.37, .47], rims: [.41, .5], dark: [.05, .88],
  cam: [.03, .4, .68, .885], sep: [.49, .68], flash: [.85, .9], open: [.905, .99] };
const OUT = [1.75, 1.05, .4];                                     // shields separate along the axis, world units

// ---------------------------------------------------------------- one camera path, start to mirror
// Round the form, up over the aperture (a rise and a swing), then down the axis through the three rims and the
// barrel to just above the mirror. One spline and one clock, so there is no stop between the parts.
const LOOK0 = new THREE.Vector3(0, .15, 0);
const C1 = new THREE.Vector3(1.4, 7.6, 1.9);
const CAMPTS = [new THREE.Vector3(0, .2, 6.6), new THREE.Vector3(1.2, 1.3, 6.8), new THREE.Vector3(3.0, 2.9, 6.1), new THREE.Vector3(4.2, 3.9, 5.0),
  new THREE.Vector3(5.6, 5.6, 2.8), C1, new THREE.Vector3(0, 5.6, .001), new THREE.Vector3(0, 3.2, 0), new THREE.Vector3(0, 1.4, 0), new THREE.Vector3(0, -.13, 0)];
const CAM = new THREE.CatmullRomCurve3(CAMPTS, false, "centripetal");
CAM.arcLengthDivisions = 2400;
const camLen = CAM.getLengths(2400);
const uAt = (i) => camLen[Math.round((i / (CAMPTS.length - 1)) * 2400)] / camLen[2400];   // path fraction at control point i
const U3 = uAt(3), U5 = uAt(5);
const camU = track([[T.cam[0], 0, 0], [T.cam[1], U3], [T.cam[2], U5, .45 * (U5 - U3) / (T.cam[2] - T.cam[1])], [T.cam[3], 1, 2.6 * (1 - U5) / (T.cam[3] - T.cam[2])]]);
const HZ = new THREE.Vector3(-C1.x, 0, -C1.z).normalize();     // "up" on screen once we look straight down
const Y = new THREE.Vector3(0, 1, 0);

if (P.has("p")) S.p = S.goal = clamp(+P.get("p"), 0, 1);
if (P.get("stage") === "sky" || P.has("target") || P.has("ra")) S.p = S.goal = 1;
if (S.mode === "frozen") { document.documentElement.classList.add("locked"); document.body.classList.add("playing"); }
const tmp = new THREE.Vector3(), dir = new THREE.Vector3(), up = new THREE.Vector3(), look = new THREE.Vector3(), white = new THREE.Color(0xffffff);
const portrait = () => W() / H() < .85;
const css = (a, b, t) => `rgb(${a.map((x, i) => Math.round(lerp(x, b[i], t))).join(",")})`;

function apply(p, dt) {
  // words
  const home = S.mode === "home", sc = home ? scr : SCR0, ink = sc.ink;
  const seq = 1 - sstep(0, .05, p), hv = seq * sc.op0;
  const hero = $("hero");
  hero.style.opacity = hv; hero.style.transform = `translateY(${-40 * (1 - seq)}px) scale(${sc.sc0})`; hero.style.visibility = hv < .01 ? "hidden" : "visible";
  hero.style.filter = sc.bl0 > .05 ? `blur(${sc.bl0.toFixed(1)}px)` : "none";
  $("more").style.opacity = sc.op0 * seq;
  BGU.uCyan.value = home ? 0 : 1;

  // page -> space: slow, from the corners; in the sequence cyan on its edge and in the corners. At home the pages set it.
  const dark = home ? sc.tone : E.sine(lin(T.dark[0], T.dark[1], p));
  BGU.uDark.value = dark;
  BGU.uSpace.value = sstep(T.open[0], T.open[0] + .04, p);
  document.body.classList.toggle("dark", dark > .42);
  bloom.strength = .45 * sstep(.42, .58, p);
  bloom.enabled = bloom.strength > .001;
  grain.uniforms.uAmt.value = Math.max(.04 * sstep(.04, .5, p), .03 * sc.tone);
  grain.uniforms.uVig.value = .5 * sstep(T.open[0], 1, p);   // only in the sky: before it the vignette would dim the cyan corners
  hemi.intensity = lerp(lerp(1.3, .55, ink), .16, sstep(.22, .74, p));
  sun.intensity = lerp(2.75, 3.2, sstep(T.metal[0], T.metal[1], p));
  starU.uFade.value = S.toolOn ? S.toolFade : sstep(.62, .86, p);
  const kfloor = sstep(.4, .66, p);
  for (const m of floorMats) m.emissive.copy(FLOOR).multiplyScalar(kfloor);

  // the form relaxes and opens into the three cones
  SHU.uTime.value = S.time;
  S.spin += dt * .08 * (1 - sstep(.04, .3, p));
  // the page's own form, place and turn (all neutral during the sequence: SCR0)
  // f: p3 p2 p4 p5 · noise rip · twist flat tilt size · x y s · earth tz
  const f = sc.f, narrow = portrait();
  SHU.uShape.value.set(f[0], f[1], f[2], f[4]); SHU.uShape2.value.set(f[3], f[5], 0, 0); SHU.uPose.value.set(f[6], f[7], f[8], f[9]);
  SHU.uEarth.value = f[13];
  // on a phone the words take the whole width, so past the first page the form waits in the upper right corner
  const away = narrow ? clamp(sc.pos, 0, 1) : 0;
  const hh = 6.6 * Math.tan(S.fov0 * D2R / 2);            // half the height of the view at the form's distance
  const bob = home ? .045 * Math.sin(S.time * .6) : 0;    // a slow breath
  let px = narrow ? (hh * W() / H() - .5) * away : f[10] * clamp(W() / H() / 1.6, .55, 1.15), py = (narrow ? (hh - .85) * away : f[11]) + bob, ps = narrow ? lerp(1, .5, away) : f[12];
  // page 4: the real plate sits on the form, a round dark lens centred behind it (the photo stays on the
  // object). Its place and size come from the plate's screen rect; it faces the camera while it is up.
  const kl = narrow ? 0 : f[15];
  let faceY = 0;
  if (kl > .001) {
    const r = $("proof").querySelector(".plate").getBoundingClientRect(), wpp = 2 * hh / H();   // world units per pixel at the form
    const lx = (r.left + r.width / 2 - W() / 2) * wpp, ly = LOOK0.y + (H() / 2 - (r.top + r.height / 2)) * wpp;   // the screen centre looks at y = LOOK0.y
    const ls = (r.width * Math.SQRT1_2 * 1.3 * wpp) / 1.6;                                     // radius 1.3 x the plate's half diagonal
    px = lerp(px, lx, kl); ps = lerp(ps, ls, kl); py = lerp(py, ly - .15 * ls, kl);           // .15: the form's own centre sits above its origin
    faceY = Math.atan2(-lx, 6.6) - .31;                                                       // the disc's tilt leaves its normal .31 rad to the right
  }
  craft.position.set(px, py, 0);
  craft.scale.setScalar(ps);
  craft.rotation.order = "ZYX";                            // the globe's tilt stays put while it turns
  craft.rotation.z = f[14];
  // the lens stays face on while it is up (the turn is taken the short way round, so it does not spin back)
  const turn = Math.atan2(Math.sin(S.spin + sc.pos * .9), Math.cos(S.spin + sc.pos * .9));
  craft.rotation.y = lerp(turn, faceY, kl);
  const M = E.sine(lin(T.form[0], T.form[1], p));
  const metal = sstep(T.metal[0], T.metal[1], p);
  const u = camU(p), ka = sstep(T.cam[1], T.cam[2], p), fall = clamp((u - U5) / (1 - U5), 0, 1);
  const sep = E.inOut(lin(T.sep[0], T.sep[1], p));
  SHU.uM.value = M;
  SHU.uCalm.value = sstep(T.calm[0], T.calm[1], p);
  SHU.uRim.value = lerp(.62, .7, ink) * (1 - sstep(.05, .2, p));   // the melting edge is for the resting form; it sharpens as the form opens
  SHU.uPaper.value.copy(PAPER_HDR).lerp(EDGE_SPACE, ink);         // on paper the edge melts into white; in space it is a faint grey light
  shells.forEach((mesh, i) => {
    const m = mesh.material, d = m.userData, o = d.own;
    const k = i === 0 ? ink : 0;                           // in space the resting form is dark and glossy
    m.color.copy(white).lerp(INKC, k).lerp(d.outC, metal);
    m.metalness = lerp(.55 * k, d.outM, metal); m.roughness = lerp(lerp(.92, .46, k), d.outR, metal);
    o.uInCol.value.copy(white).lerp(d.inC, metal);
    o.uInMetal.value = metal; o.uInRough.value = lerp(.92, d.inR, metal);
    m.envMapIntensity = lerp(lerp(.25, .95, k), lerp(.5, .22, ka), metal);
    mesh.visible = i === 0 || M > 0;                       // at home the inner layers are hidden inside the outer one
    // a closed form draws its front faces only (thin edges, like the lens, would speckle with the back faces); an open
    // cone needs both sides
    const side = M > 0 ? THREE.DoubleSide : THREE.FrontSide;
    if (m.side !== side) { m.side = side; m.needsUpdate = true; }
    mesh.position.y = dress[i].position.y = OUT[i] * sep;
  });

  // the rest of the craft grows out of the foot of the cones; the panel unfolds; hairlines and rims come up
  const kc = E.out(lin(T.core[0], T.core[1], p)), kb = E.inOut(lin(T.bus[0], T.bus[1], p)), kp = E.inOut(lin(T.panel[0], T.panel[1], p));
  gCore.a.visible = kc > 0; gCore.a.scale.setScalar(Math.max(kc, 1e-3));
  // once we are well inside the cones the bus is out of sight; lower down its edges would show through the gap at the cone feet
  gBus.a.visible = kb > 0 && fall < .45; gBus.a.scale.setScalar(Math.max(kb, 1e-3)); gBus.a.rotation.y = .7 * (1 - kb);
  gBus.a.position.y = 1.15 - .55 * sep / GS;
  gSol.a.scale.setScalar(Math.max(kp, 1e-3)); gSol.a.rotation.y = 1.1 * (1 - kp);
  const kband = sstep(T.bands[0], T.bands[1], p);
  bandMat.opacity = kband;
  gOuter.visible = kband > 0; gOuter.position.y = OUT[0] * sep / GS;
  const kd = sstep(T.dress[0], T.dress[1], p);
  lineMat.opacity = .16 * kd; seamMat.opacity = .07 * kd; footMat.opacity = kd;
  dress.forEach((g) => (g.visible = kd > 0));
  const rim = sstep(T.rims[0], T.rims[1], p);
  rimMats.forEach((m) => { m.opacity = rim; m.color.setScalar(1.4 + 2.2 * fall); });
  renderer.toneMappingExposure = fall <= 0 ? lerp(1.05, .7, ka) : lerp(.7, .58, fall);
  // the mirror lights up as we come down the barrel: the screen brightens into the flash instead of going black
  mirrorMat.color.copy(GLOW).multiplyScalar(5 * Math.pow(clamp((fall - .5) / .5, 0, 1), 2));

  // the flash, then the white field stop opens onto the sky
  const veil = $("veil");
  const ko = lin(T.open[0], T.open[1], p);
  if (p < T.open[0]) {
    const kfl = lin(T.flash[0], T.flash[1], p);
    veil.style.background = css([214, 246, 251], [255, 255, 255], kfl);
    veil.style.opacity = Math.pow(kfl, 1.6);
  } else {
    const kE = E.out(ko);
    const cx = view.cx * W(), cy = view.cy * H(), Rm = Math.hypot(Math.max(cx, W() - cx), Math.max(cy, H() - cy)) + 40;
    const r = Rm * kE, c = css([255, 255, 255], [7, 9, 11], 1 - (1 - kE) ** 1.3).slice(4, -1);
    veil.style.opacity = ko >= 1 ? 0 : 1;
    veil.style.background = `radial-gradient(circle at ${cx}px ${cy}px, rgba(0,0,0,0) ${r}px, rgba(${c},${1 - ko * .4}) ${r + 1.5}px)`;
  }
  gridMat.opacity = S.toolOn ? S.toolGrid : .06 * sstep(T.open[0], 1, p);
  $("credit").style.opacity = (1 - sstep(.6, .7, p) + sstep(.95, 1, p)) * (home ? sc.op0 : 1);

  if (p >= T.open[0]) {
    // we are the telescope, at the centre of the sphere
    craft.visible = false;
    renderer.toneMappingExposure = 1.05;
    if (!S.toolOn) {                                     // while a field is open the plate screen holds the view
      Object.assign(view, { cx: .5, cy: portrait() ? .38 : .42 });
      S.sky.fov = lerp(64, 50, E.out(ko));
      if (!S.drag) S.sky.dir.copy(slerpV(S.sky.dir, S.sky.goal, S.live ? .06 : 1));
    }
    camera.position.set(0, 0, 0); camera.up.set(0, 1, 0);
    camera.lookAt(tmp.copy(S.sky.dir).multiplyScalar(100));
    camera.fov = S.sky.fov;
    BGU.uExp.value = renderer.toneMappingExposure;
    return;
  }
  craft.visible = true;
  Object.assign(view, { cx: .5, cy: .5 });
  BGU.uExp.value = renderer.toneMappingExposure;
  const pos = CAM.getPointAt(u);
  look.set(0, lerp(LOOK0.y, 0, ka) - .6 * fall, 0);
  dir.copy(look).sub(pos);
  if (dir.lengthSq() < 1e-6) dir.set(0, -1, 0);
  dir.normalize();
  up.copy(Y).lerp(HZ, sstep(.55, .97, -dir.y)).normalize().applyAxisAngle(Y, 1.1 * fall * fall);
  camera.position.copy(pos); camera.up.copy(up); camera.lookAt(tmp.copy(pos).add(dir));
  camera.fov = lerp(lerp(S.fov0, 46, ka), 62, fall);
}

// ---------------------------------------------------------------- Begin plays the sequence
// No scroll driving: the first screen is an ordinary section and the page below is free for more.
const DUR = (REDUCED ? 1600 : 12000) / Math.max(.1, +P.get("speed") || 1);
function lockStage(on) {
  document.documentElement.classList.toggle("locked", on);
  document.body.classList.toggle("playing", on);
  requestAnimationFrame(fit);
}
function begin() {
  if (S.mode !== "home") return;
  scrollTo(0, 0); S.sy = 0; readScroll(0);
  lockStage(true);
  S.mode = "play"; S.t0 = performance.now(); S.p = 0;
}
function backToStart() {
  if (S.mode === "home" || S.resetting) return;
  S.resetting = true;
  const f = $("fade");
  f.classList.add("on");
  setTimeout(() => {
    tool.kill(); leaveSky();
    history.replaceState({}, "", location.pathname);
    S.mode = "home"; S.p = 0; S.spin = 0;
    S.sky.dir.copy(radec(281, -4)); S.sky.goal.copy(S.sky.dir);
    $("veil").style.opacity = 0;
    lockStage(false);
    requestAnimationFrame(() => requestAnimationFrame(() => { f.classList.remove("on"); S.resetting = false; $("begin").focus({ preventScroll: true }); }));
  }, REDUCED ? 0 : 380);
}
// a short cut through the page colour, for jumps that would otherwise show a hard change
function through(night, fn) {
  const f = $("fade");
  f.classList.toggle("night", night); f.classList.add("on");
  setTimeout(() => { fn(); requestAnimationFrame(() => requestAnimationFrame(() => f.classList.remove("on"))); }, REDUCED ? 0 : 380);
}
// a target chosen on the page: straight to the sky beside it, then the zoom into its field
function openDirect(id) {
  const t = TARGETS.find((x) => x.id === id);
  if (!t || S.mode !== "home" || S.resetting) return;
  through(true, () => {
    lockStage(true);
    S.mode = "frozen"; S.p = 1;
    S.sky.dir.copy(radec(t.ra, clamp(t.dec + 16, -80, 80))); S.sky.goal.copy(S.sky.dir);
    S.pending = t;
  });
}
$("begin").addEventListener("click", begin);
// the last page: back to the first one
for (const b of document.querySelectorAll("[data-top]")) b.addEventListener("click", () => scrollTo({ top: 0, behavior: REDUCED ? "auto" : "smooth" }));
for (const b of document.querySelectorAll("[data-begin]")) b.addEventListener("click", () => S.mode === "home" && through(false, begin));
for (const b of document.querySelectorAll("[data-open]")) b.addEventListener("click", () => openDirect(b.dataset.open));
$("back").addEventListener("click", backToStart);
addEventListener("keydown", (e) => {
  if (tool.onKey(e)) return;
  if (e.key === "Escape" && S.mode !== "home" && document.activeElement?.id !== "q") backToStart();
});

// ---------------------------------------------------------------- sky: markers, picks, search, drag
const TARGETS = [
  { id: "barnard", name: "Barnard's Star", ra: 269.452, dec: 4.693, note: "The fastest-moving star on the sky: about 10″ a year." },
  { id: "eps-indi-b", name: "Epsilon Indi B", ra: 330.84, dec: -56.786, note: "A pair of brown dwarfs 12 light years away." },
  { id: "wise-0855", name: "WISE 0855", ra: 133.795, dec: -7.245, note: "The coldest known brown dwarf. Opens in band W (4.3–5.0 µm); at short wavelengths the nearby source is a fixed Gaia star." },
  { id: "rw-aur", name: "RW Aurigae", ra: 76.956, dec: 30.401, note: "A young star whose brightness changes." },
  { id: "r-dra", name: "R Draconis", ra: 248.17, dec: 66.75, note: "A long-period variable." },
];
const markerEls = new Map();
for (const t of TARGETS) {
  const b = document.createElement("button");
  b.className = "mk"; b.innerHTML = `<i></i><span>${t.name}</span>`; b.tabIndex = -1;
  b.onclick = () => lookAt(t);
  $("markers").appendChild(b); markerEls.set(t.id, b);
  const li = document.createElement("li"), pb = document.createElement("button");
  pb.type = "button"; pb.textContent = t.name;
  pb.onmouseenter = pb.onfocus = () => preview(t);
  pb.onmouseleave = pb.onblur = () => preview(null);
  pb.onclick = () => lookAt(t);
  li.appendChild(pb); $("picks").appendChild(li);
}
let previewFrom = null;
function preview(t) {
  for (const [id, el] of markerEls) el.classList.toggle("hot", !!t && id === t.id);
  if (t) { if (!previewFrom) previewFrom = S.sky.goal.clone(); S.sky.goal.copy(radec(t.ra, t.dec)); }
  else if (previewFrom) { S.sky.goal.copy(previewFrom); previewFrom = null; }
}
function placeMarkers() {
  const fwd = new THREE.Vector3(); camera.getWorldDirection(fwd);
  for (const t of TARGETS) {
    const el = markerEls.get(t.id), d = radec(t.ra, t.dec);
    if (!S.live || d.dot(fwd) < .2) { el.style.opacity = 0; el.style.pointerEvents = "none"; continue; }
    tmp.copy(d).multiplyScalar(100).project(camera);
    const x = (tmp.x + 1) / 2 * W(), y = (1 - tmp.y) / 2 * H();
    const inside = x > 10 && x < W() - 10 && y > 10 && y < H() - 10;
    el.style.opacity = inside ? 1 : 0; el.style.pointerEvents = inside ? "auto" : "none";
    el.style.transform = `translate(${x}px, ${y}px)`;
  }
}
const hms = (ra) => { const h = ra / 15, Hh = Math.floor(h), m = (h - Hh) * 60, M = Math.floor(m), sec = (m - M) * 60; return `${String(Hh).padStart(2, "0")}h ${String(M).padStart(2, "0")}m ${sec.toFixed(0).padStart(2, "0")}s`; };
const dms = (dec) => { const a = Math.abs(dec), D = Math.floor(a), M = Math.floor((a - D) * 60); return `${dec < 0 ? "−" : "+"}${String(D).padStart(2, "0")}° ${String(M).padStart(2, "0")}′`; };
function lookAt(t) {
  previewFrom = null;
  for (const [id, el] of markerEls) el.classList.toggle("hot", id === t.id);
  S.sky.goal.copy(radec(t.ra, t.dec));
  const known = TARGETS.find((x) => x.id === t.id);
  $("card-kind").textContent = known ? "Challenge target" : "Position";
  $("card-name").textContent = t.name;
  $("card-coords").textContent = `${hms(t.ra)}  ${dms(t.dec)}`;
  $("card-note").textContent = known ? known.note : "";
  const go = $("card-go");
  go.href = known ? `?target=${known.id}` : `?ra=${t.ra.toFixed(4)}&dec=${t.dec.toFixed(4)}`;
  go.onclick = (e) => { e.preventDefault(); $("card").hidden = true; known ? tool.open(known, { fast: FAST }) : openFree(t.ra, t.dec); };
  $("card").hidden = false;
}
$("card-x").addEventListener("click", () => ($("card").hidden = true));
function parseTarget(q) {
  const s = q.trim();
  if (!s) return null;
  const hit = TARGETS.find((t) => t.name.toLowerCase().replace(/[^a-z0-9]/g, "") === s.toLowerCase().replace(/[^a-z0-9]/g, "") || t.id === s.toLowerCase());
  if (hit) return hit;
  const nums = s.replace(/[hmsd°'":,]/gi, " ").trim().split(/\s+/).map(Number);
  if (nums.some(Number.isNaN)) return { error: "Name lookup is not wired in this study. Try a challenge target, or RA Dec in degrees." };
  let ra, dec;
  if (nums.length === 2) [ra, dec] = nums;
  else if (nums.length === 6) {
    ra = (nums[0] + nums[1] / 60 + nums[2] / 3600) * 15;
    const sg = /^\s*-|\s-/.test(s.split(/\s+/).slice(3).join(" ")) || nums[3] < 0 || Object.is(nums[3], -0) ? -1 : 1;
    dec = sg * (Math.abs(nums[3]) + nums[4] / 60 + nums[5] / 3600);
  } else return { error: "Give RA and Dec, e.g. 133.79 -7.24, or 08 55 10.8 -07 14 42." };
  if (!(ra >= 0 && ra < 360 && dec >= -90 && dec <= 90)) return { error: "RA runs 0 to 360, Dec -90 to 90." };
  return { id: `${ra.toFixed(4)},${dec.toFixed(4)}`, name: "Custom position", ra, dec };
}
$("search").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = parseTarget($("q").value);
  if (!t) return;
  if (t.error) { $("askmsg").textContent = t.error; return; }
  $("askmsg").textContent = "";
  if (TARGETS.some((x) => x.id === t.id)) lookAt(t); else openFree(t.ra, t.dec);
});
// any spot of sky: the live server builds its frames (the five challenge targets come from the gallery files)
// is the live server there? one quick health check at start (none when config.js sets SPX_API to ""); the hover hint reads it
findServer().then((u) => { S.server = !!u; });
async function openFree(ra, dec) {
  const msg = $("askmsg");
  msg.textContent = "";
  const near = TARGETS.find((t) => radec(t.ra, t.dec).dot(radec(ra, dec)) > Math.cos(.6 * D2R));
  if (near) return tool.open(near, { fast: FAST });
  if (S.server !== true) {
    msg.textContent = "Looking for the live server…";
    S.server = !!(await findServer());
    msg.textContent = "";
  }
  if (!S.server && window.SPX_API) {   // a server is configured but did not answer (it can be starting up)
    msg.innerHTML = `The live server is not answering right now. Try again in a moment; the five targets work meanwhile. <button type="button" class="retry">Try again</button>`;
    msg.querySelector(".retry").onclick = () => openFree(ra, dec);
    return;
  }
  if (!S.server) { msg.innerHTML = `This position needs the live server, which is not part of this site. The five targets work here. <a href="https://github.com/Ayz345/Sci-Tech-SPHEREx-Blink#run-it" target="_blank" rel="noopener">How to run the server</a>`; return; }
  tool.open({ ra, dec, free: true }, { fast: FAST });
}
// where on the sky a point of the screen looks
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
function screenToRaDec(x, y) {
  const r = cv.getBoundingClientRect();
  ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  return toRaDec(ray.ray.direction);
}
function hover(x, y) {
  const cu = $("cursor"), [ra, dec] = screenToRaDec(x, y);
  const near = TARGETS.find((t) => radec(t.ra, t.dec).dot(radec(ra, dec)) > Math.cos(1.2 * D2R));
  cu.style.transform = `translate(${x}px, ${y}px)`;
  cu.querySelector("span").innerHTML = `<b>${hms(ra)}  ${dms(dec)}</b>${near ? `click to open ${near.name}` : S.server === false && !window.SPX_API ? "this position needs the live server" : "click to zoom in here"}`;
  cu.classList.add("on");
}
async function pick(x, y) {
  const [ra, dec] = screenToRaDec(x, y);
  $("card").hidden = true; $("cursor").classList.remove("on");
  const near = TARGETS.find((t) => radec(t.ra, t.dec).dot(radec(ra, dec)) > Math.cos(1.2 * D2R));
  if (near) return tool.open(near, { fast: FAST });
  openFree(ra, dec);
}
{
  const c = $("gl");
  let px = 0, py = 0, x0 = 0, y0 = 0, moved = 0;
  // a press that moves less than 6 px is a click (zoom in there); more is a drag (look around)
  c.addEventListener("pointerdown", (e) => { if (!S.live || S.toolOn) return; S.drag = true; px = x0 = e.clientX; py = y0 = e.clientY; moved = 0; c.setPointerCapture(e.pointerId); previewFrom = null; });
  c.addEventListener("pointermove", (e) => {
    if (S.live && !S.toolOn && !S.drag && e.pointerType !== "touch") hover(e.clientX, e.clientY);
    if (!S.drag) return;
    moved = Math.max(moved, Math.hypot(e.clientX - x0, e.clientY - y0));
    if (moved < 6) return;
    document.body.classList.add("dragging");
    const k = (S.sky.fov * D2R) / H();
    let [ra, dec] = toRaDec(S.sky.dir);
    ra += (e.clientX - px) * k / D2R / Math.max(.2, Math.cos(dec * D2R));
    dec = clamp(dec + (e.clientY - py) * k / D2R, -84, 84);
    px = e.clientX; py = e.clientY;
    S.sky.dir.copy(radec(ra, dec)); S.sky.goal.copy(S.sky.dir);
  });
  const end = () => { S.drag = false; document.body.classList.remove("dragging"); };
  c.addEventListener("pointerup", (e) => { const click = S.drag && moved < 6; end(); if (click && S.live && !S.toolOn) pick(e.clientX, e.clientY); });
  c.addEventListener("pointercancel", end);
  c.addEventListener("pointerleave", () => $("cursor").classList.remove("on"));
}
function enterSky() {
  if (S.live) return;
  S.live = true; S.mode = "sky"; document.body.classList.add("sky");
  const t = S.pending; S.pending = null;
  if (t) requestAnimationFrame(() => tool.open(t, { fast: FAST }));
}
const tool = createTool({ $, S, view, radec, toRaDec, slerpV, tween, E, lerp, clamp, W, H, portrait, REDUCED });
{
  // a shared link (?target=) lands in the sky beside the target and zooms in
  const t = TARGETS.find((x) => x.id === P.get("target"));
  if (t) { S.pending = t; S.sky.dir.copy(radec(t.ra, clamp(t.dec + 16, -80, 80))); S.sky.goal.copy(S.sky.dir); }
  const ra = +P.get("ra"), dec = +P.get("dec");
  if (!t && P.has("ra") && Number.isFinite(ra) && Number.isFinite(dec)) { S.pending = { ra, dec, free: true }; S.sky.dir.copy(radec(ra, clamp(dec + 16, -80, 80))); S.sky.goal.copy(S.sky.dir); }
}

// ---------------------------------------------------------------- page 4: the real frames of WISE 0855, blinking
const proof = { e: null, band: null, ok: [], i: 0, t: 0 };
(async () => {
  try {
    const e = await loadTarget("wise-0855"), band = e.m.default_band || e.m.bands[0].id;
    await loadBand(e, band);
    Object.assign(proof, { e, band, ok: e.m.epochs.map((_, i) => i).filter((i) => playable(e, band, i, false)) });
    drawProof();
  } catch { $("pcap").textContent = "The gallery frames could not be read."; }
})();
function drawProof() {
  const { e, band, ok } = proof;
  if (!e || !ok.length) return;
  const i = ok[proof.i % ok.length], ov = $("pov");
  paint($("pcv"), e, band, i);
  ov.replaceChildren(); drawMarker(ov, e, i, "#fff");
  setUpper($("pcap"), `${e.m.name} · ${e.m.bands.find((b) => b.id === band).label} · pass ${i + 1} of ${e.m.epochs.length} · ${fmtDate(dateOf(e, band, i))} · ${usedOf(e, band, i)} exposures stacked`);
}
function leaveSky() { S.live = false; S.drag = false; document.body.classList.remove("sky", "dragging"); $("card").hidden = true; $("cursor").classList.remove("on"); $("askmsg").textContent = ""; }

// ---------------------------------------------------------------- loop
function fit() {
  const w = W(), h = H(), a = w / h;
  S.fov0 = Math.max(35, 2 * Math.atan(Math.tan(20 * D2R) / a) / D2R);
  renderer.setSize(w, h, false); composer.setSize(w, h);
  grain.uniforms.uRes.value.set(w, h);
  BGU.uAspect.value = a;
  measurePages();
  tool.onResize();
}
fit();
new ResizeObserver(fit).observe(cv);
addEventListener("resize", fit);
// ?page=3 starts at a page (review)
S.sy = 0;
if (P.has("page") && S.mode === "home") { scrollTo(0, clamp(+P.get("page") | 0, 0, PAGES.length - 1) * STEP * innerHeight); S.sy = scrollY; }
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(.05, (now - S.last) / 1000); S.last = now;
  for (const t of tweens) { const r = clamp((now - t.t0) / t.ms, 0, 1); t.fn(t.ease(r), r); if (r >= 1) { tweens.delete(t); t.res(); } }
  if (!REDUCED) S.time += dt;
  if (S.mode === "home") {
    S.sy += (scrollY - S.sy) * (REDUCED ? 1 : 1 - Math.exp(-dt * 9));
    if (Math.abs(scrollY - S.sy) < .5) S.sy = scrollY;
    readScroll(S.sy);
    // the proof plate blinks while its page is near
    if (proof.e && Math.abs(scr.pos - 4) < .8 && now - proof.t > (REDUCED ? 2600 : 1100)) { proof.t = now; proof.i++; drawProof(); }
  }
  BGU.uTime.value = S.time; starU.uTime.value = S.time; grain.uniforms.uTime.value = S.time;
  if (S.mode === "play") S.p = clamp((now - S.t0) / DUR, 0, 1);
  if (S.p >= 1 && (S.mode === "play" || S.mode === "frozen")) enterSky();
  apply(S.p, dt);
  applyView();
  placeMarkers();
  composer.render();
}
requestAnimationFrame(frame);
window.__v5 = { S, T, CAM, camU, scr, tool, STEP, screenToRaDec };
