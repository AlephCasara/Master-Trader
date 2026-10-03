export const themeColor=(name:string):string=>getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const THEME_CHANGE='themechange';
