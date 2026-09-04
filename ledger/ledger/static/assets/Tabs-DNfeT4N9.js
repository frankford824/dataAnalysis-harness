import{I as ee,J as c,aX as gt,A as _,aY as ht,aZ as ye,a_ as xt,a$ as Q,b0 as mt,E as yt,a1 as Ct,Y as $e,aj as St,K as wt,F as Tt,aC as Pt,b1 as Rt,b2 as zt,z as U,av as $t,H as r,a6 as s,G as S,a5 as $,a7 as Lt,a8 as ne,a4 as Ce,b3 as oe,M as Bt,N as Le,$ as Wt,w as ie,o as _t,b4 as At,aE as Et,aa as jt,b5 as Se,b6 as kt,b7 as Mt,aP as Vt,b8 as Ht,ar as se,O as k,b9 as J,ba as It,ab as Ot,a3 as M,a2 as Z}from"./index-DM7GKOPI.js";import{A as Gt}from"./Add-C0H1jBRP.js";const Ft=ye(".v-x-scroll",{overflow:"auto",scrollbarWidth:"none"},[ye("&::-webkit-scrollbar",{width:0,height:0})]),Dt=ee({name:"XScroll",props:{disabled:Boolean,onScroll:Function},setup(){const e=_(null);function n(l){!(l.currentTarget.offsetWidth<l.currentTarget.scrollWidth)||l.deltaY===0||(l.currentTarget.scrollLeft+=l.deltaY+l.deltaX,l.preventDefault())}const i=ht();return Ft.mount({id:"vueuc/x-scroll",head:!0,anchorMetaName:gt,ssr:i}),Object.assign({selfRef:e,handleWheel:n},{scrollTo(...l){var m;(m=e.value)===null||m===void 0||m.scrollTo(...l)}})},render(){return c("div",{ref:"selfRef",onScroll:this.onScroll,onWheel:this.disabled?void 0:this.handleWheel,class:"v-x-scroll"},this.$slots)}});var Nt=/\s/;function Xt(e){for(var n=e.length;n--&&Nt.test(e.charAt(n)););return n}var Ut=/^\s+/;function Yt(e){return e&&e.slice(0,Xt(e)+1).replace(Ut,"")}var we=NaN,Kt=/^[-+]0x[0-9a-f]+$/i,qt=/^0b[01]+$/i,Jt=/^0o[0-7]+$/i,Zt=parseInt;function Te(e){if(typeof e=="number")return e;if(xt(e))return we;if(Q(e)){var n=typeof e.valueOf=="function"?e.valueOf():e;e=Q(n)?n+"":n}if(typeof e!="string")return e===0?e:+e;e=Yt(e);var i=qt.test(e);return i||Jt.test(e)?Zt(e.slice(2),i?2:8):Kt.test(e)?we:+e}var le=function(){return mt.Date.now()},Qt="Expected a function",ea=Math.max,ta=Math.min;function aa(e,n,i){var f,l,m,v,p,g,h=0,y=!1,P=!1,R=!0;if(typeof e!="function")throw new TypeError(Qt);n=Te(n)||0,Q(i)&&(y=!!i.leading,P="maxWait"in i,m=P?ea(Te(i.maxWait)||0,n):m,R="trailing"in i?!!i.trailing:R);function x(d){var W=f,I=l;return f=l=void 0,h=d,v=e.apply(I,W),v}function C(d){return h=d,p=setTimeout(B,n),y?x(d):v}function w(d){var W=d-g,I=d-h,O=n-W;return P?ta(O,m-I):O}function z(d){var W=d-g,I=d-h;return g===void 0||W>=n||W<0||P&&I>=m}function B(){var d=le();if(z(d))return L(d);p=setTimeout(B,w(d))}function L(d){return p=void 0,R&&f?x(d):(f=l=void 0,v)}function V(){p!==void 0&&clearTimeout(p),h=0,f=g=l=p=void 0}function j(){return p===void 0?v:L(le())}function u(){var d=le(),W=z(d);if(f=arguments,l=this,g=d,W){if(p===void 0)return C(g);if(P)return clearTimeout(p),p=setTimeout(B,n),x(g)}return p===void 0&&(p=setTimeout(B,n)),v}return u.cancel=V,u.flush=j,u}var ra="Expected a function";function na(e,n,i){var f=!0,l=!0;if(typeof e!="function")throw new TypeError(ra);return Q(i)&&(f="leading"in i?!!i.leading:f,l="trailing"in i?!!i.trailing:l),aa(e,n,{leading:f,maxWait:n,trailing:l})}const oa={tabFontSizeSmall:"14px",tabFontSizeMedium:"14px",tabFontSizeLarge:"16px",tabGapSmallLine:"36px",tabGapMediumLine:"36px",tabGapLargeLine:"36px",tabGapSmallLineVertical:"8px",tabGapMediumLineVertical:"8px",tabGapLargeLineVertical:"8px",tabPaddingSmallLine:"6px 0",tabPaddingMediumLine:"10px 0",tabPaddingLargeLine:"14px 0",tabPaddingVerticalSmallLine:"6px 12px",tabPaddingVerticalMediumLine:"8px 16px",tabPaddingVerticalLargeLine:"10px 20px",tabGapSmallBar:"36px",tabGapMediumBar:"36px",tabGapLargeBar:"36px",tabGapSmallBarVertical:"8px",tabGapMediumBarVertical:"8px",tabGapLargeBarVertical:"8px",tabPaddingSmallBar:"4px 0",tabPaddingMediumBar:"6px 0",tabPaddingLargeBar:"10px 0",tabPaddingVerticalSmallBar:"6px 12px",tabPaddingVerticalMediumBar:"8px 16px",tabPaddingVerticalLargeBar:"10px 20px",tabGapSmallCard:"4px",tabGapMediumCard:"4px",tabGapLargeCard:"4px",tabGapSmallCardVertical:"4px",tabGapMediumCardVertical:"4px",tabGapLargeCardVertical:"4px",tabPaddingSmallCard:"8px 16px",tabPaddingMediumCard:"10px 20px",tabPaddingLargeCard:"12px 24px",tabPaddingSmallSegment:"4px 0",tabPaddingMediumSegment:"6px 0",tabPaddingLargeSegment:"8px 0",tabPaddingVerticalLargeSegment:"0 8px",tabPaddingVerticalSmallCard:"8px 12px",tabPaddingVerticalMediumCard:"10px 16px",tabPaddingVerticalLargeCard:"12px 20px",tabPaddingVerticalSmallSegment:"0 4px",tabPaddingVerticalMediumSegment:"0 6px",tabGapSmallSegment:"0",tabGapMediumSegment:"0",tabGapLargeSegment:"0",tabGapSmallSegmentVertical:"0",tabGapMediumSegmentVertical:"0",tabGapLargeSegmentVertical:"0",panePaddingSmall:"8px 0 0 0",panePaddingMedium:"12px 0 0 0",panePaddingLarge:"16px 0 0 0",closeSize:"18px",closeIconSize:"14px"};function ia(e){const{textColor2:n,primaryColor:i,textColorDisabled:f,closeIconColor:l,closeIconColorHover:m,closeIconColorPressed:v,closeColorHover:p,closeColorPressed:g,tabColor:h,baseColor:y,dividerColor:P,fontWeight:R,textColor1:x,borderRadius:C,fontSize:w,fontWeightStrong:z}=e;return Object.assign(Object.assign({},oa),{colorSegment:h,tabFontSizeCard:w,tabTextColorLine:x,tabTextColorActiveLine:i,tabTextColorHoverLine:i,tabTextColorDisabledLine:f,tabTextColorSegment:x,tabTextColorActiveSegment:n,tabTextColorHoverSegment:n,tabTextColorDisabledSegment:f,tabTextColorBar:x,tabTextColorActiveBar:i,tabTextColorHoverBar:i,tabTextColorDisabledBar:f,tabTextColorCard:x,tabTextColorHoverCard:x,tabTextColorActiveCard:i,tabTextColorDisabledCard:f,barColor:i,closeIconColor:l,closeIconColorHover:m,closeIconColorPressed:v,closeColorHover:p,closeColorPressed:g,closeBorderRadius:C,tabColor:h,tabColorSegment:y,tabBorderColor:P,tabFontWeightActive:R,tabFontWeight:R,tabBorderRadius:C,paneTextColor:n,fontWeightStrong:z})}const sa={common:yt,self:ia},fe=Ct("n-tabs"),Be={tab:[String,Number,Object,Function],name:{type:[String,Number],required:!0},disabled:Boolean,displayDirective:{type:String,default:"if"},closable:{type:Boolean,default:void 0},tabProps:Object,label:[String,Number,Object,Function]},pa=ee({__TAB_PANE__:!0,name:"TabPane",alias:["TabPanel"],props:Be,slots:Object,setup(e){const n=$e(fe,null);return n||St("tab-pane","`n-tab-pane` must be placed inside `n-tabs`."),{style:n.paneStyleRef,class:n.paneClassRef,mergedClsPrefix:n.mergedClsPrefixRef}},render(){return c("div",{class:[`${this.mergedClsPrefix}-tab-pane`,this.class],style:this.style},this.$slots)}}),la=Object.assign({internalLeftPadded:Boolean,internalAddable:Boolean,internalCreatedByPane:Boolean},$t(Be,["displayDirective"])),ce=ee({__TAB__:!0,inheritAttrs:!1,name:"Tab",props:la,setup(e){const{mergedClsPrefixRef:n,valueRef:i,typeRef:f,closableRef:l,tabStyleRef:m,addTabStyleRef:v,tabClassRef:p,addTabClassRef:g,tabChangeIdRef:h,onBeforeLeaveRef:y,triggerRef:P,handleAdd:R,activateTab:x,handleClose:C}=$e(fe);return{trigger:P,mergedClosable:U(()=>{if(e.internalAddable)return!1;const{closable:w}=e;return w===void 0?l.value:w}),style:m,addStyle:v,tabClass:p,addTabClass:g,clsPrefix:n,value:i,type:f,handleClose(w){w.stopPropagation(),!e.disabled&&C(e.name)},activateTab(){if(e.disabled)return;if(e.internalAddable){R();return}const{name:w}=e,z=++h.id;if(w!==i.value){const{value:B}=y;B?Promise.resolve(B(e.name,i.value)).then(L=>{L&&h.id===z&&x(w)}):x(w)}}}},render(){const{internalAddable:e,clsPrefix:n,name:i,disabled:f,label:l,tab:m,value:v,mergedClosable:p,trigger:g,$slots:{default:h}}=this,y=l??m;return c("div",{class:`${n}-tabs-tab-wrapper`},this.internalLeftPadded?c("div",{class:`${n}-tabs-tab-pad`}):null,c("div",Object.assign({key:i,"data-name":i,"data-disabled":f?!0:void 0},wt({class:[`${n}-tabs-tab`,v===i&&`${n}-tabs-tab--active`,f&&`${n}-tabs-tab--disabled`,p&&`${n}-tabs-tab--closable`,e&&`${n}-tabs-tab--addable`,e?this.addTabClass:this.tabClass],onClick:g==="click"?this.activateTab:void 0,onMouseenter:g==="hover"?this.activateTab:void 0,style:e?this.addStyle:this.style},this.internalCreatedByPane?this.tabProps||{}:this.$attrs)),c("span",{class:`${n}-tabs-tab__label`},e?c(Tt,null,c("div",{class:`${n}-tabs-tab__height-placeholder`}," "),c(Pt,{clsPrefix:n},{default:()=>c(Gt,null)})):h?h():typeof y=="object"?y:Rt(y??i)),p&&this.type==="card"?c(zt,{clsPrefix:n,class:`${n}-tabs-tab__close`,onClick:this.handleClose,disabled:f}):null))}}),da=r("tabs",`
 box-sizing: border-box;
 width: 100%;
 display: flex;
 flex-direction: column;
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
`,[s("segment-type",[r("tabs-rail",[S("&.transition-disabled",[r("tabs-capsule",`
 transition: none;
 `)])])]),s("top",[r("tab-pane",`
 padding: var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left);
 `)]),s("left",[r("tab-pane",`
 padding: var(--n-pane-padding-right) var(--n-pane-padding-bottom) var(--n-pane-padding-left) var(--n-pane-padding-top);
 `)]),s("left, right",`
 flex-direction: row;
 `,[r("tabs-bar",`
 width: 2px;
 right: 0;
 transition:
 top .2s var(--n-bezier),
 max-height .2s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `),r("tabs-tab",`
 padding: var(--n-tab-padding-vertical); 
 `)]),s("right",`
 flex-direction: row-reverse;
 `,[r("tab-pane",`
 padding: var(--n-pane-padding-left) var(--n-pane-padding-top) var(--n-pane-padding-right) var(--n-pane-padding-bottom);
 `),r("tabs-bar",`
 left: 0;
 `)]),s("bottom",`
 flex-direction: column-reverse;
 justify-content: flex-end;
 `,[r("tab-pane",`
 padding: var(--n-pane-padding-bottom) var(--n-pane-padding-right) var(--n-pane-padding-top) var(--n-pane-padding-left);
 `),r("tabs-bar",`
 top: 0;
 `)]),r("tabs-rail",`
 position: relative;
 padding: 3px;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 background-color: var(--n-color-segment);
 transition: background-color .3s var(--n-bezier);
 display: flex;
 align-items: center;
 `,[r("tabs-capsule",`
 border-radius: var(--n-tab-border-radius);
 position: absolute;
 pointer-events: none;
 background-color: var(--n-tab-color-segment);
 box-shadow: 0 1px 3px 0 rgba(0, 0, 0, .08);
 transition: transform 0.3s var(--n-bezier);
 `),r("tabs-tab-wrapper",`
 flex-basis: 0;
 flex-grow: 1;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[r("tabs-tab",`
 overflow: hidden;
 border-radius: var(--n-tab-border-radius);
 width: 100%;
 display: flex;
 align-items: center;
 justify-content: center;
 `,[s("active",`
 font-weight: var(--n-font-weight-strong);
 color: var(--n-tab-text-color-active);
 `),S("&:hover",`
 color: var(--n-tab-text-color-hover);
 `)])])]),s("flex",[r("tabs-nav",`
 width: 100%;
 position: relative;
 `,[r("tabs-wrapper",`
 width: 100%;
 `,[r("tabs-tab",`
 margin-right: 0;
 `)])])]),r("tabs-nav",`
 box-sizing: border-box;
 line-height: 1.5;
 display: flex;
 transition: border-color .3s var(--n-bezier);
 `,[$("prefix, suffix",`
 display: flex;
 align-items: center;
 `),$("prefix","padding-right: 16px;"),$("suffix","padding-left: 16px;")]),s("top, bottom",[S(">",[r("tabs-nav",[r("tabs-nav-scroll-wrapper",[S("&::before",`
 top: 0;
 bottom: 0;
 left: 0;
 width: 20px;
 `),S("&::after",`
 top: 0;
 bottom: 0;
 right: 0;
 width: 20px;
 `),s("shadow-start",[S("&::before",`
 box-shadow: inset 10px 0 8px -8px rgba(0, 0, 0, .12);
 `)]),s("shadow-end",[S("&::after",`
 box-shadow: inset -10px 0 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),s("left, right",[r("tabs-nav-scroll-content",`
 flex-direction: column;
 `),S(">",[r("tabs-nav",[r("tabs-nav-scroll-wrapper",[S("&::before",`
 top: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),S("&::after",`
 bottom: 0;
 left: 0;
 right: 0;
 height: 20px;
 `),s("shadow-start",[S("&::before",`
 box-shadow: inset 0 10px 8px -8px rgba(0, 0, 0, .12);
 `)]),s("shadow-end",[S("&::after",`
 box-shadow: inset 0 -10px 8px -8px rgba(0, 0, 0, .12);
 `)])])])])]),r("tabs-nav-scroll-wrapper",`
 flex: 1;
 position: relative;
 overflow: hidden;
 `,[r("tabs-nav-y-scroll",`
 height: 100%;
 width: 100%;
 overflow-y: auto; 
 scrollbar-width: none;
 `,[S("&::-webkit-scrollbar, &::-webkit-scrollbar-track-piece, &::-webkit-scrollbar-thumb",`
 width: 0;
 height: 0;
 display: none;
 `)]),S("&::before, &::after",`
 transition: box-shadow .3s var(--n-bezier);
 pointer-events: none;
 content: "";
 position: absolute;
 z-index: 1;
 `)]),r("tabs-nav-scroll-content",`
 display: flex;
 position: relative;
 min-width: 100%;
 min-height: 100%;
 width: fit-content;
 box-sizing: border-box;
 `),r("tabs-wrapper",`
 display: inline-flex;
 flex-wrap: nowrap;
 position: relative;
 `),r("tabs-tab-wrapper",`
 display: flex;
 flex-wrap: nowrap;
 flex-shrink: 0;
 flex-grow: 0;
 `),r("tabs-tab",`
 cursor: pointer;
 white-space: nowrap;
 flex-wrap: nowrap;
 display: inline-flex;
 align-items: center;
 color: var(--n-tab-text-color);
 font-size: var(--n-tab-font-size);
 background-clip: padding-box;
 padding: var(--n-tab-padding);
 transition:
 box-shadow .3s var(--n-bezier),
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier);
 `,[s("disabled",{cursor:"not-allowed"}),$("close",`
 margin-left: 6px;
 transition:
 background-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 `),$("label",`
 display: flex;
 align-items: center;
 z-index: 1;
 `)]),r("tabs-bar",`
 position: absolute;
 bottom: 0;
 height: 2px;
 border-radius: 1px;
 background-color: var(--n-bar-color);
 transition:
 left .2s var(--n-bezier),
 max-width .2s var(--n-bezier),
 opacity .3s var(--n-bezier),
 background-color .3s var(--n-bezier);
 `,[S("&.transition-disabled",`
 transition: none;
 `),s("disabled",`
 background-color: var(--n-tab-text-color-disabled)
 `)]),r("tabs-pane-wrapper",`
 position: relative;
 overflow: hidden;
 transition: max-height .2s var(--n-bezier);
 `),r("tab-pane",`
 color: var(--n-pane-text-color);
 width: 100%;
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 opacity .2s var(--n-bezier);
 left: 0;
 right: 0;
 top: 0;
 `,[S("&.next-transition-leave-active, &.prev-transition-leave-active, &.next-transition-enter-active, &.prev-transition-enter-active",`
 transition:
 color .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 transform .2s var(--n-bezier),
 opacity .2s var(--n-bezier);
 `),S("&.next-transition-leave-active, &.prev-transition-leave-active",`
 position: absolute;
 `),S("&.next-transition-enter-from, &.prev-transition-leave-to",`
 transform: translateX(32px);
 opacity: 0;
 `),S("&.next-transition-leave-to, &.prev-transition-enter-from",`
 transform: translateX(-32px);
 opacity: 0;
 `),S("&.next-transition-leave-from, &.next-transition-enter-to, &.prev-transition-leave-from, &.prev-transition-enter-to",`
 transform: translateX(0);
 opacity: 1;
 `)]),r("tabs-tab-pad",`
 box-sizing: border-box;
 width: var(--n-tab-gap);
 flex-grow: 0;
 flex-shrink: 0;
 `),s("line-type, bar-type",[r("tabs-tab",`
 font-weight: var(--n-tab-font-weight);
 box-sizing: border-box;
 vertical-align: bottom;
 `,[S("&:hover",{color:"var(--n-tab-text-color-hover)"}),s("active",`
 color: var(--n-tab-text-color-active);
 font-weight: var(--n-tab-font-weight-active);
 `),s("disabled",{color:"var(--n-tab-text-color-disabled)"})])]),r("tabs-nav",[s("line-type",[s("top",[$("prefix, suffix",`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),r("tabs-nav-scroll-content",`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),r("tabs-bar",`
 bottom: -1px;
 `)]),s("left",[$("prefix, suffix",`
 border-right: 1px solid var(--n-tab-border-color);
 `),r("tabs-nav-scroll-content",`
 border-right: 1px solid var(--n-tab-border-color);
 `),r("tabs-bar",`
 right: -1px;
 `)]),s("right",[$("prefix, suffix",`
 border-left: 1px solid var(--n-tab-border-color);
 `),r("tabs-nav-scroll-content",`
 border-left: 1px solid var(--n-tab-border-color);
 `),r("tabs-bar",`
 left: -1px;
 `)]),s("bottom",[$("prefix, suffix",`
 border-top: 1px solid var(--n-tab-border-color);
 `),r("tabs-nav-scroll-content",`
 border-top: 1px solid var(--n-tab-border-color);
 `),r("tabs-bar",`
 top: -1px;
 `)]),$("prefix, suffix",`
 transition: border-color .3s var(--n-bezier);
 `),r("tabs-nav-scroll-content",`
 transition: border-color .3s var(--n-bezier);
 `),r("tabs-bar",`
 border-radius: 0;
 `)]),s("card-type",[$("prefix, suffix",`
 transition: border-color .3s var(--n-bezier);
 `),r("tabs-pad",`
 flex-grow: 1;
 transition: border-color .3s var(--n-bezier);
 `),r("tabs-tab-pad",`
 transition: border-color .3s var(--n-bezier);
 `),r("tabs-tab",`
 font-weight: var(--n-tab-font-weight);
 border: 1px solid var(--n-tab-border-color);
 background-color: var(--n-tab-color);
 box-sizing: border-box;
 position: relative;
 vertical-align: bottom;
 display: flex;
 justify-content: space-between;
 font-size: var(--n-tab-font-size);
 color: var(--n-tab-text-color);
 `,[s("addable",`
 padding-left: 8px;
 padding-right: 8px;
 font-size: 16px;
 justify-content: center;
 `,[$("height-placeholder",`
 width: 0;
 font-size: var(--n-tab-font-size);
 `),Lt("disabled",[S("&:hover",`
 color: var(--n-tab-text-color-hover);
 `)])]),s("closable","padding-right: 8px;"),s("active",`
 background-color: #0000;
 font-weight: var(--n-tab-font-weight-active);
 color: var(--n-tab-text-color-active);
 `),s("disabled","color: var(--n-tab-text-color-disabled);")])]),s("left, right",`
 flex-direction: column; 
 `,[$("prefix, suffix",`
 padding: var(--n-tab-padding-vertical);
 `),r("tabs-wrapper",`
 flex-direction: column;
 `),r("tabs-tab-wrapper",`
 flex-direction: column;
 `,[r("tabs-tab-pad",`
 height: var(--n-tab-gap-vertical);
 width: 100%;
 `)])]),s("top",[s("card-type",[r("tabs-scroll-padding","border-bottom: 1px solid var(--n-tab-border-color);"),$("prefix, suffix",`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),r("tabs-tab",`
 border-top-left-radius: var(--n-tab-border-radius);
 border-top-right-radius: var(--n-tab-border-radius);
 `,[s("active",`
 border-bottom: 1px solid #0000;
 `)]),r("tabs-tab-pad",`
 border-bottom: 1px solid var(--n-tab-border-color);
 `),r("tabs-pad",`
 border-bottom: 1px solid var(--n-tab-border-color);
 `)])]),s("left",[s("card-type",[r("tabs-scroll-padding","border-right: 1px solid var(--n-tab-border-color);"),$("prefix, suffix",`
 border-right: 1px solid var(--n-tab-border-color);
 `),r("tabs-tab",`
 border-top-left-radius: var(--n-tab-border-radius);
 border-bottom-left-radius: var(--n-tab-border-radius);
 `,[s("active",`
 border-right: 1px solid #0000;
 `)]),r("tabs-tab-pad",`
 border-right: 1px solid var(--n-tab-border-color);
 `),r("tabs-pad",`
 border-right: 1px solid var(--n-tab-border-color);
 `)])]),s("right",[s("card-type",[r("tabs-scroll-padding","border-left: 1px solid var(--n-tab-border-color);"),$("prefix, suffix",`
 border-left: 1px solid var(--n-tab-border-color);
 `),r("tabs-tab",`
 border-top-right-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[s("active",`
 border-left: 1px solid #0000;
 `)]),r("tabs-tab-pad",`
 border-left: 1px solid var(--n-tab-border-color);
 `),r("tabs-pad",`
 border-left: 1px solid var(--n-tab-border-color);
 `)])]),s("bottom",[s("card-type",[r("tabs-scroll-padding","border-top: 1px solid var(--n-tab-border-color);"),$("prefix, suffix",`
 border-top: 1px solid var(--n-tab-border-color);
 `),r("tabs-tab",`
 border-bottom-left-radius: var(--n-tab-border-radius);
 border-bottom-right-radius: var(--n-tab-border-radius);
 `,[s("active",`
 border-top: 1px solid #0000;
 `)]),r("tabs-tab-pad",`
 border-top: 1px solid var(--n-tab-border-color);
 `),r("tabs-pad",`
 border-top: 1px solid var(--n-tab-border-color);
 `)])])])]),de=na,ba=Object.assign(Object.assign({},Le.props),{value:[String,Number],defaultValue:[String,Number],trigger:{type:String,default:"click"},type:{type:String,default:"bar"},closable:Boolean,justifyContent:String,size:String,placement:{type:String,default:"top"},tabStyle:[String,Object],tabClass:String,addTabStyle:[String,Object],addTabClass:String,barWidth:Number,paneClass:String,paneStyle:[String,Object],paneWrapperClass:String,paneWrapperStyle:[String,Object],addable:[Boolean,Object],tabsPadding:{type:Number,default:0},animated:Boolean,onBeforeLeave:Function,onAdd:Function,"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onClose:[Function,Array],labelSize:String,activeName:[String,Number],onActiveNameChange:[Function,Array]}),ua=ee({name:"Tabs",props:ba,slots:Object,setup(e,{slots:n}){var i,f,l,m;const{mergedClsPrefixRef:v,inlineThemeDisabled:p,mergedComponentPropsRef:g}=Bt(e),h=Le("Tabs","-tabs",da,sa,e,v),y=_(null),P=_(null),R=_(null),x=_(null),C=_(null),w=_(null),z=_(!0),B=_(!0),L=Se(e,["labelSize","size"]),V=U(()=>{var t,a;if(L.value)return L.value;const o=(a=(t=g?.value)===null||t===void 0?void 0:t.Tabs)===null||a===void 0?void 0:a.size;return o||"medium"}),j=Se(e,["activeName","value"]),u=_((f=(i=j.value)!==null&&i!==void 0?i:e.defaultValue)!==null&&f!==void 0?f:n.default?(m=(l=ne(n.default())[0])===null||l===void 0?void 0:l.props)===null||m===void 0?void 0:m.name:null),d=Wt(j,u),W={id:0},I=U(()=>{if(!(!e.justifyContent||e.type==="card"))return{display:"flex",justifyContent:e.justifyContent}});ie(d,()=>{W.id=0,Y(),ue()});function O(){var t;const{value:a}=d;return a===null?null:(t=y.value)===null||t===void 0?void 0:t.querySelector(`[data-name="${a}"]`)}function We(t){if(e.type==="card")return;const{value:a}=P;if(!a)return;const o=a.style.opacity==="0";if(t){const b=`${v.value}-tabs-bar--disabled`,{barWidth:T,placement:A}=e;if(t.dataset.disabled==="true"?a.classList.add(b):a.classList.remove(b),["top","bottom"].includes(A)){if(pe(["top","maxHeight","height"]),typeof T=="number"&&t.offsetWidth>=T){const E=Math.floor((t.offsetWidth-T)/2)+t.offsetLeft;a.style.left=`${E}px`,a.style.maxWidth=`${T}px`}else a.style.left=`${t.offsetLeft}px`,a.style.maxWidth=`${t.offsetWidth}px`;a.style.width="8192px",o&&(a.style.transition="none"),a.offsetWidth,o&&(a.style.transition="",a.style.opacity="1")}else{if(pe(["left","maxWidth","width"]),typeof T=="number"&&t.offsetHeight>=T){const E=Math.floor((t.offsetHeight-T)/2)+t.offsetTop;a.style.top=`${E}px`,a.style.maxHeight=`${T}px`}else a.style.top=`${t.offsetTop}px`,a.style.maxHeight=`${t.offsetHeight}px`;a.style.height="8192px",o&&(a.style.transition="none"),a.offsetHeight,o&&(a.style.transition="",a.style.opacity="1")}}}function _e(){if(e.type==="card")return;const{value:t}=P;t&&(t.style.opacity="0")}function pe(t){const{value:a}=P;if(a)for(const o of t)a.style[o]=""}function Y(){if(e.type==="card")return;const t=O();t?We(t):_e()}function ue(){var t;const a=(t=C.value)===null||t===void 0?void 0:t.$el;if(!a)return;const o=O();if(!o)return;const{scrollLeft:b,offsetWidth:T}=a,{offsetLeft:A,offsetWidth:E}=o;b>A?a.scrollTo({top:0,left:A,behavior:"smooth"}):A+E>b+T&&a.scrollTo({top:0,left:A+E-T,behavior:"smooth"})}const K=_(null);let te=0,H=null;function Ae(t){const a=K.value;if(a){te=t.getBoundingClientRect().height;const o=`${te}px`,b=()=>{a.style.height=o,a.style.maxHeight=o};H?(b(),H(),H=null):H=b}}function Ee(t){const a=K.value;if(a){const o=t.getBoundingClientRect().height,b=()=>{document.body.offsetHeight,a.style.maxHeight=`${o}px`,a.style.height=`${Math.max(te,o)}px`};H?(H(),H=null,b()):H=b}}function je(){const t=K.value;if(t){t.style.maxHeight="",t.style.height="";const{paneWrapperStyle:a}=e;if(typeof a=="string")t.style.cssText=a;else if(a){const{maxHeight:o,height:b}=a;o!==void 0&&(t.style.maxHeight=o),b!==void 0&&(t.style.height=b)}}}const ve={value:[]},ge=_("next");function ke(t){const a=d.value;let o="next";for(const b of ve.value){if(b===a)break;if(b===t){o="prev";break}}ge.value=o,Me(t)}function Me(t){const{onActiveNameChange:a,onUpdateValue:o,"onUpdate:value":b}=e;a&&Z(a,t),o&&Z(o,t),b&&Z(b,t),u.value=t}function Ve(t){const{onClose:a}=e;a&&Z(a,t)}function he(){const{value:t}=P;if(!t)return;const a="transition-disabled";t.classList.add(a),Y(),t.classList.remove(a)}const G=_(null);function ae({transitionDisabled:t}){const a=y.value;if(!a)return;t&&a.classList.add("transition-disabled");const o=O();o&&G.value&&(G.value.style.width=`${o.offsetWidth}px`,G.value.style.height=`${o.offsetHeight}px`,G.value.style.transform=`translateX(${o.offsetLeft-kt(getComputedStyle(a).paddingLeft)}px)`,t&&G.value.offsetWidth),t&&a.classList.remove("transition-disabled")}ie([d],()=>{e.type==="segment"&&se(()=>{ae({transitionDisabled:!1})})}),_t(()=>{e.type==="segment"&&ae({transitionDisabled:!0})});let xe=0;function He(t){var a;if(t.contentRect.width===0&&t.contentRect.height===0||xe===t.contentRect.width)return;xe=t.contentRect.width;const{type:o}=e;if((o==="line"||o==="bar")&&he(),o!=="segment"){const{placement:b}=e;re((b==="top"||b==="bottom"?(a=C.value)===null||a===void 0?void 0:a.$el:w.value)||null)}}const Ie=de(He,64);ie([()=>e.justifyContent,()=>e.size],()=>{se(()=>{const{type:t}=e;(t==="line"||t==="bar")&&he()})});const F=_(!1);function Oe(t){var a;const{target:o,contentRect:{width:b,height:T}}=t,A=o.parentElement.parentElement.offsetWidth,E=o.parentElement.parentElement.offsetHeight,{placement:N}=e;if(!F.value)N==="top"||N==="bottom"?A<b&&(F.value=!0):E<T&&(F.value=!0);else{const{value:X}=x;if(!X)return;N==="top"||N==="bottom"?A-b>X.$el.offsetWidth&&(F.value=!1):E-T>X.$el.offsetHeight&&(F.value=!1)}re(((a=C.value)===null||a===void 0?void 0:a.$el)||null)}const Ge=de(Oe,64);function Fe(){const{onAdd:t}=e;t&&t(),se(()=>{const a=O(),{value:o}=C;!a||!o||o.scrollTo({left:a.offsetLeft,top:0,behavior:"smooth"})})}function re(t){if(!t)return;const{placement:a}=e;if(a==="top"||a==="bottom"){const{scrollLeft:o,scrollWidth:b,offsetWidth:T}=t;z.value=o<=0,B.value=o+T>=b}else{const{scrollTop:o,scrollHeight:b,offsetHeight:T}=t;z.value=o<=0,B.value=o+T>=b}}const De=de(t=>{re(t.target)},64);Ot(fe,{triggerRef:M(e,"trigger"),tabStyleRef:M(e,"tabStyle"),tabClassRef:M(e,"tabClass"),addTabStyleRef:M(e,"addTabStyle"),addTabClassRef:M(e,"addTabClass"),paneClassRef:M(e,"paneClass"),paneStyleRef:M(e,"paneStyle"),mergedClsPrefixRef:v,typeRef:M(e,"type"),closableRef:M(e,"closable"),valueRef:d,tabChangeIdRef:W,onBeforeLeaveRef:M(e,"onBeforeLeave"),activateTab:ke,handleClose:Ve,handleAdd:Fe}),At(()=>{Y(),ue()}),Et(()=>{const{value:t}=R;if(!t)return;const{value:a}=v,o=`${a}-tabs-nav-scroll-wrapper--shadow-start`,b=`${a}-tabs-nav-scroll-wrapper--shadow-end`;z.value?t.classList.remove(o):t.classList.add(o),B.value?t.classList.remove(b):t.classList.add(b)});const Ne={syncBarPosition:()=>{Y()}},Xe=()=>{ae({transitionDisabled:!0})},me=U(()=>{const{value:t}=V,{type:a}=e,o={card:"Card",bar:"Bar",line:"Line",segment:"Segment"}[a],b=`${t}${o}`,{self:{barColor:T,closeIconColor:A,closeIconColorHover:E,closeIconColorPressed:N,tabColor:X,tabBorderColor:Ue,paneTextColor:Ye,tabFontWeight:Ke,tabBorderRadius:qe,tabFontWeightActive:Je,colorSegment:Ze,fontWeightStrong:Qe,tabColorSegment:et,closeSize:tt,closeIconSize:at,closeColorHover:rt,closeColorPressed:nt,closeBorderRadius:ot,[k("panePadding",t)]:q,[k("tabPadding",b)]:it,[k("tabPaddingVertical",b)]:st,[k("tabGap",b)]:lt,[k("tabGap",`${b}Vertical`)]:dt,[k("tabTextColor",a)]:bt,[k("tabTextColorActive",a)]:ct,[k("tabTextColorHover",a)]:ft,[k("tabTextColorDisabled",a)]:pt,[k("tabFontSize",t)]:ut},common:{cubicBezierEaseInOut:vt}}=h.value;return{"--n-bezier":vt,"--n-color-segment":Ze,"--n-bar-color":T,"--n-tab-font-size":ut,"--n-tab-text-color":bt,"--n-tab-text-color-active":ct,"--n-tab-text-color-disabled":pt,"--n-tab-text-color-hover":ft,"--n-pane-text-color":Ye,"--n-tab-border-color":Ue,"--n-tab-border-radius":qe,"--n-close-size":tt,"--n-close-icon-size":at,"--n-close-color-hover":rt,"--n-close-color-pressed":nt,"--n-close-border-radius":ot,"--n-close-icon-color":A,"--n-close-icon-color-hover":E,"--n-close-icon-color-pressed":N,"--n-tab-color":X,"--n-tab-font-weight":Ke,"--n-tab-font-weight-active":Je,"--n-tab-padding":it,"--n-tab-padding-vertical":st,"--n-tab-gap":lt,"--n-tab-gap-vertical":dt,"--n-pane-padding-left":J(q,"left"),"--n-pane-padding-right":J(q,"right"),"--n-pane-padding-top":J(q,"top"),"--n-pane-padding-bottom":J(q,"bottom"),"--n-font-weight-strong":Qe,"--n-tab-color-segment":et}}),D=p?jt("tabs",U(()=>`${V.value[0]}${e.type[0]}`),me,e):void 0;return Object.assign({mergedClsPrefix:v,mergedValue:d,renderedNames:new Set,segmentCapsuleElRef:G,tabsPaneWrapperRef:K,tabsElRef:y,barElRef:P,addTabInstRef:x,xScrollInstRef:C,scrollWrapperElRef:R,addTabFixed:F,tabWrapperStyle:I,handleNavResize:Ie,mergedSize:V,handleScroll:De,handleTabsResize:Ge,cssVars:p?void 0:me,themeClass:D?.themeClass,animationDirection:ge,renderNameListRef:ve,yScrollElRef:w,handleSegmentResize:Xe,onAnimationBeforeLeave:Ae,onAnimationEnter:Ee,onAnimationAfterEnter:je,onRender:D?.onRender},Ne)},render(){const{mergedClsPrefix:e,type:n,placement:i,addTabFixed:f,addable:l,mergedSize:m,renderNameListRef:v,onRender:p,paneWrapperClass:g,paneWrapperStyle:h,$slots:{default:y,prefix:P,suffix:R}}=this;p?.();const x=y?ne(y()).filter(u=>u.type.__TAB_PANE__===!0):[],C=y?ne(y()).filter(u=>u.type.__TAB__===!0):[],w=!C.length,z=n==="card",B=n==="segment",L=!z&&!B&&this.justifyContent;v.value=[];const V=()=>{const u=c("div",{style:this.tabWrapperStyle,class:`${e}-tabs-wrapper`},L?null:c("div",{class:`${e}-tabs-scroll-padding`,style:i==="top"||i==="bottom"?{width:`${this.tabsPadding}px`}:{height:`${this.tabsPadding}px`}}),w?x.map((d,W)=>(v.value.push(d.props.name),be(c(ce,Object.assign({},d.props,{internalCreatedByPane:!0,internalLeftPadded:W!==0&&(!L||L==="center"||L==="start"||L==="end")}),d.children?{default:d.children.tab}:void 0)))):C.map((d,W)=>(v.value.push(d.props.name),be(W!==0&&!L?ze(d):d))),!f&&l&&z?Re(l,(w?x.length:C.length)!==0):null,L?null:c("div",{class:`${e}-tabs-scroll-padding`,style:{width:`${this.tabsPadding}px`}}));return c("div",{ref:"tabsElRef",class:`${e}-tabs-nav-scroll-content`},z&&l?c(oe,{onResize:this.handleTabsResize},{default:()=>u}):u,z?c("div",{class:`${e}-tabs-pad`}):null,z?null:c("div",{ref:"barElRef",class:`${e}-tabs-bar`}))},j=B?"top":i;return c("div",{class:[`${e}-tabs`,this.themeClass,`${e}-tabs--${n}-type`,`${e}-tabs--${m}-size`,L&&`${e}-tabs--flex`,`${e}-tabs--${j}`],style:this.cssVars},c("div",{class:[`${e}-tabs-nav--${n}-type`,`${e}-tabs-nav--${j}`,`${e}-tabs-nav`]},Ce(P,u=>u&&c("div",{class:`${e}-tabs-nav__prefix`},u)),B?c(oe,{onResize:this.handleSegmentResize},{default:()=>c("div",{class:`${e}-tabs-rail`,ref:"tabsElRef"},c("div",{class:`${e}-tabs-capsule`,ref:"segmentCapsuleElRef"},c("div",{class:`${e}-tabs-wrapper`},c("div",{class:`${e}-tabs-tab`}))),w?x.map((u,d)=>(v.value.push(u.props.name),c(ce,Object.assign({},u.props,{internalCreatedByPane:!0,internalLeftPadded:d!==0}),u.children?{default:u.children.tab}:void 0))):C.map((u,d)=>(v.value.push(u.props.name),d===0?u:ze(u))))}):c(oe,{onResize:this.handleNavResize},{default:()=>c("div",{class:`${e}-tabs-nav-scroll-wrapper`,ref:"scrollWrapperElRef"},["top","bottom"].includes(j)?c(Dt,{ref:"xScrollInstRef",onScroll:this.handleScroll},{default:V}):c("div",{class:`${e}-tabs-nav-y-scroll`,onScroll:this.handleScroll,ref:"yScrollElRef"},V()))}),f&&l&&z?Re(l,!0):null,Ce(R,u=>u&&c("div",{class:`${e}-tabs-nav__suffix`},u))),w&&(this.animated&&(j==="top"||j==="bottom")?c("div",{ref:"tabsPaneWrapperRef",style:h,class:[`${e}-tabs-pane-wrapper`,g]},Pe(x,this.mergedValue,this.renderedNames,this.onAnimationBeforeLeave,this.onAnimationEnter,this.onAnimationAfterEnter,this.animationDirection)):Pe(x,this.mergedValue,this.renderedNames)))}});function Pe(e,n,i,f,l,m,v){const p=[];return e.forEach(g=>{const{name:h,displayDirective:y,"display-directive":P}=g.props,R=C=>y===C||P===C,x=n===h;if(g.key!==void 0&&(g.key=h),x||R("show")||R("show:lazy")&&i.has(h)){i.has(h)||i.add(h);const C=!R("if");p.push(C?Mt(g,[[It,x]]):g)}}),v?c(Vt,{name:`${v}-transition`,onBeforeLeave:f,onEnter:l,onAfterEnter:m},{default:()=>p}):p}function Re(e,n){return c(ce,{ref:"addTabInstRef",key:"__addable",name:"__addable",internalCreatedByPane:!0,internalAddable:!0,internalLeftPadded:n,disabled:typeof e=="object"&&e.disabled})}function ze(e){const n=Ht(e);return n.props?n.props.internalLeftPadded=!0:n.props={internalLeftPadded:!0},n}function be(e){return Array.isArray(e.dynamicProps)?e.dynamicProps.includes("internalLeftPadded")||e.dynamicProps.push("internalLeftPadded"):e.dynamicProps=["internalLeftPadded"],e}export{ua as _,pa as a};
