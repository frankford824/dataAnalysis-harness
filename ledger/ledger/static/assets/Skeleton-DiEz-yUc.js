import{ar as V,as as E,X as p,V as H,Y as N,T as b,ag as O,at as T,F as $,Z as j,$ as w,J as k,au as A,av as y}from"./index-CnCdZA5v.js";let S=!1;function F(){if(V&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function L(e){const{heightSmall:i,heightMedium:r,heightLarge:s,borderRadius:a}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:a,heightSmall:i,heightMedium:r,heightLarge:s}}const I={common:E,self:L},J=p([H("skeleton",`
 height: 1em;
 width: 100%;
 transition:
 --n-color-start .3s var(--n-bezier),
 --n-color-end .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 animation: 2s skeleton-loading infinite cubic-bezier(0.36, 0, 0.64, 1);
 background-color: var(--n-color-start);
 `),p("@keyframes skeleton-loading",`
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
 `)]),K=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),W=N({name:"Skeleton",inheritAttrs:!1,props:K,setup(e){F();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=j(e),s=k(()=>{var n,t;return e.size||((t=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||t===void 0?void 0:t.size)}),a=w("Skeleton","-skeleton",J,I,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,t;const m=a.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:_,colorEnd:x,borderRadius:C}=h;let l;const{circle:d,sharp:P,round:B,width:o,height:c,text:f,animated:R}=e,v=s.value;v!==void 0&&(l=h[A("height",v)]);const u=d?(n=o??c)!==null&&n!==void 0?n:l:o,g=(t=d?o??c:c)!==null&&t!==void 0?t:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":B?"4096px":P?"":C,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:R?"":"none","--n-bezier":z,"--n-color-start":_,"--n-color-end":x}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:s}=this,a=b("div",O({class:`${r}-skeleton`,style:i},s));return e>1?b($,null,T(e,null).map(n=>[a,`
`])):a}});export{W as _};
