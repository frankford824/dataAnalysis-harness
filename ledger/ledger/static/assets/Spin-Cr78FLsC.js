import{K as S,W as h,U as p,bA as C,V as f,G as x,H as r,bG as T,a3 as $,O as k,P as v,Z as w,aq as O,p as m,q as R,bH as _,av as j,aj as P,at as B}from"./index-B4toxQZv.js";function H(e){const{opacityDisabled:a,heightTiny:t,heightSmall:s,heightMedium:l,heightLarge:n,heightHuge:c,primaryColor:o,fontSize:i}=e;return{fontSize:i,textColor:o,sizeTiny:t,sizeSmall:s,sizeMedium:l,sizeLarge:n,sizeHuge:c,color:o,opacitySpinning:a}}const V={common:S,self:H},L=h([h("@keyframes spin-rotate",`
 from {
 transform: rotate(0);
 }
 to {
 transform: rotate(360deg);
 }
 `),p("spin-container",`
 position: relative;
 `,[p("spin-body",`
 position: absolute;
 top: 50%;
 left: 50%;
 transform: translateX(-50%) translateY(-50%);
 `,[C()])]),p("spin-body",`
 display: inline-flex;
 align-items: center;
 justify-content: center;
 flex-direction: column;
 `),p("spin",`
 display: inline-flex;
 height: var(--n-size);
 width: var(--n-size);
 font-size: var(--n-size);
 color: var(--n-color);
 `,[f("rotate",`
 animation: spin-rotate 2s linear infinite;
 `)]),p("spin-description",`
 display: inline-block;
 font-size: var(--n-font-size);
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 margin-top: 8px;
 `),p("spin-content",`
 opacity: 1;
 transition: opacity .3s var(--n-bezier);
 pointer-events: all;
 `,[f("spinning",`
 user-select: none;
 -webkit-user-select: none;
 pointer-events: none;
 opacity: var(--n-opacity-spinning);
 `)])]),W={small:20,medium:18,large:16},I=Object.assign(Object.assign(Object.assign({},v.props),{contentClass:String,contentStyle:[Object,String],description:String,size:{type:[String,Number],default:"medium"},show:{type:Boolean,default:!0},rotate:{type:Boolean,default:!0},spinning:{type:Boolean,validator:()=>!0,default:void 0},delay:Number}),_),D=x({name:"Spin",props:I,slots:Object,setup(e){const{mergedClsPrefixRef:a,inlineThemeDisabled:t}=k(e),s=v("Spin","-spin",L,V,e,a),l=m(()=>{const{size:i}=e,{common:{cubicBezierEaseInOut:d},self:u}=s.value,{opacitySpinning:g,color:y,textColor:b}=u,z=typeof i=="number"?j(i):u[P("size",i)];return{"--n-bezier":d,"--n-opacity-spinning":g,"--n-size":z,"--n-color":y,"--n-text-color":b}}),n=t?w("spin",m(()=>{const{size:i}=e;return typeof i=="number"?String(i):i[0]}),l,e):void 0,c=B(e,["spinning","show"]),o=R(!1);return O(i=>{let d;if(c.value){const{delay:u}=e;if(u){d=window.setTimeout(()=>{o.value=!0},u),i(()=>{clearTimeout(d)});return}}o.value=c.value}),{mergedClsPrefix:a,active:o,mergedStrokeWidth:m(()=>{const{strokeWidth:i}=e;if(i!==void 0)return i;const{size:d}=e;return W[typeof d=="number"?"medium":d]}),cssVars:t?void 0:l,themeClass:n?.themeClass,onRender:n?.onRender}},render(){var e,a;const{$slots:t,mergedClsPrefix:s,description:l}=this,n=t.icon&&this.rotate,c=(l||t.description)&&r("div",{class:`${s}-spin-description`},l||((e=t.description)===null||e===void 0?void 0:e.call(t))),o=t.icon?r("div",{class:[`${s}-spin-body`,this.themeClass]},r("div",{class:[`${s}-spin`,n&&`${s}-spin--rotate`],style:t.default?"":this.cssVars},t.icon()),c):r("div",{class:[`${s}-spin-body`,this.themeClass]},r(T,{clsPrefix:s,style:t.default?"":this.cssVars,stroke:this.stroke,"stroke-width":this.mergedStrokeWidth,radius:this.radius,scale:this.scale,class:`${s}-spin`}),c);return(a=this.onRender)===null||a===void 0||a.call(this),t.default?r("div",{class:[`${s}-spin-container`,this.themeClass],style:this.cssVars},r("div",{class:[`${s}-spin-content`,this.active&&`${s}-spin-content--spinning`,this.contentClass],style:this.contentStyle},t),r($,{name:"fade-in-transition"},{default:()=>this.active?o:null})):o}});export{D as _};
