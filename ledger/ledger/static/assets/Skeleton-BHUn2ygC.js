import{Q as V,R as E,S as v,T as H,U as N,V as b,W as O,X as T,F as $,Y as j,Z as w,I as k,$ as A,a0 as y}from"./index-BQVQ0l1i.js";let S=!1;function F(){if(V&&window.CSS&&!S&&(S=!0,"registerProperty"in window?.CSS))try{CSS.registerProperty({name:"--n-color-start",syntax:"<color>",inherits:!1,initialValue:"#0000"}),CSS.registerProperty({name:"--n-color-end",syntax:"<color>",inherits:!1,initialValue:"#0000"})}catch{}}function I(e){const{heightSmall:i,heightMedium:r,heightLarge:a,borderRadius:s}=e;return{color:"#eee",colorEnd:"#ddd",borderRadius:s,heightSmall:i,heightMedium:r,heightLarge:a}}const L={common:E,self:I},W=v([H("skeleton",`
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
 `)]),K=Object.assign(Object.assign({},w.props),{text:Boolean,round:Boolean,circle:Boolean,height:[String,Number],width:[String,Number],size:String,repeat:{type:Number,default:1},animated:{type:Boolean,default:!0},sharp:{type:Boolean,default:!0}}),Q=N({name:"Skeleton",inheritAttrs:!1,props:K,setup(e){F();const{mergedClsPrefixRef:i,mergedComponentPropsRef:r}=j(e),a=k(()=>{var n,o;return e.size||((o=(n=r?.value)===null||n===void 0?void 0:n.Skeleton)===null||o===void 0?void 0:o.size)}),s=w("Skeleton","-skeleton",W,L,e,i);return{mergedClsPrefix:i,style:k(()=>{var n,o;const m=s.value,{common:{cubicBezierEaseInOut:z}}=m,h=m.self,{color:_,colorEnd:x,borderRadius:C}=h;let l;const{circle:d,sharp:P,round:R,width:t,height:c,text:f,animated:B}=e,p=a.value;p!==void 0&&(l=h[A("height",p)]);const u=d?(n=t??c)!==null&&n!==void 0?n:l:t,g=(o=d?t??c:c)!==null&&o!==void 0?o:l;return{display:f?"inline-block":"",verticalAlign:f?"-0.125em":"",borderRadius:d?"50%":R?"4096px":P?"":C,width:typeof u=="number"?y(u):u,height:typeof g=="number"?y(g):g,animation:B?"":"none","--n-bezier":z,"--n-color-start":_,"--n-color-end":x}})}},render(){const{repeat:e,style:i,mergedClsPrefix:r,$attrs:a}=this,s=b("div",O({class:`${r}-skeleton`,style:i},a));return e>1?b($,null,T(e,null).map(n=>[s,`
`])):s}});export{Q as _};
