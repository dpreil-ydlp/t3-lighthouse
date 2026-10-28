const SESSION_COOKIE='lighthouse_session';

function decodeBase64Url(value:string){
  const padded=value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4);
  return atob(padded);
}

export async function validEdgeSession(value:string|undefined){
  if(!value)return false;
  const [payload,signature,...extra]=value.split('.');
  if(!payload||!signature||extra.length)return false;
  const env=process.env as Record<string,string|undefined>;
  const secret=env['DASHBOARD_SESSION_KEY'];
  if(!secret)return false;
  try {
    const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(secret),{name:'HMAC',hash:'SHA-256'},false,['verify']);
    const signed=Uint8Array.from(decodeBase64Url(signature),char=>char.charCodeAt(0));
    const valid=await crypto.subtle.verify('HMAC',key,signed,new TextEncoder().encode(payload));
    if(!valid)return false;
    const claims=JSON.parse(decodeBase64Url(payload));
    const now=Math.floor(Date.now()/1000);
    return claims.kind==='user'&&typeof claims.sub==='string'&&typeof claims.sid==='string'&&
      Number.isInteger(claims.iat)&&claims.iat<=now&&Number.isInteger(claims.exp)&&claims.exp>now;
  } catch { return false }
}

export {SESSION_COOKIE};
