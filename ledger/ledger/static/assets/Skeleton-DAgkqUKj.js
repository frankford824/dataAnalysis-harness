import{aP as E,d as H,o as v,m as N,e as O,h as b,aF as V,aQ as j,K as A,g as F,w,k,aR as K,aS as y}from"./index-BvfAXjDA.js";let S=!1;function L(){if(E&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function T(e){const{heightSmall:i,heightMedium:r,heightLarge:s,borderRadius:a}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:a,heightSmall:i,heightMedium:r,heightLarge:s}}const $={common:H,self:T},I=v([N("skeleton",`
 height: 1em;
 width: 100%;
 transition:
 --n-color-start .3s var(--n-bezier),
 --n-color-end .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 animation: 2s skeleton-loading infinite cubic-bezier(0.36, 0, 0.64, 1);
 background-color: var(--n-color-start);
 `),v("@keyframes skeleton-loading",`
 0% {
 background: var(--n-color-start);
 }
 40% {
 background: var(--n-color-end);
 }
 80% {
 background: var(--n-color-start);
 }
 100% {
 background: var(--n-color-start);
 }
 `)]),M=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),W=O({name:"Skeleton",inheritAttrs:!1,props:M,setup(e){L();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=F(e),s=k(()=>{var n,o;return e.size||((o=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||o===void 0?void 0:o.size)}),a=w("Skeleton","-skeleton",I,$,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,o;const m=a.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:P,colorEnd:_,borderRadius:x}=h;let l;const{circle:d,sharp:C,round:R,width:t,height:c,text:f,animated:B}=e,p=s.value;p!==void 0&&(l=h[K("height",p)]);const u=d?(n=t??c)!==null&&n!==void 0?n:l:t,g=(o=d?t??c:c)!==null&&o!==void 0?o:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":R?"4096px":C?"":x,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:B?"":"none","--n-bezier":z,"--n-color-start":P,"--n-color-end":_}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:s}=this,a=b("div",V({class:`${r}-skeleton`,style:i},s));return e>1?b(A,null,j(e,null).map(n=>[a,`
`])):a}});export{W as _};
