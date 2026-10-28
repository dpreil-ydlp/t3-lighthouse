import crypto from 'node:crypto';
import fs from 'node:fs';

export const SESSION_COOKIE = 'lighthouse_session';
export const SESSION_TTL_SECONDS = 30 * 24 * 60 * 60;

export type SessionClaims = {sub:string;kind:'user';sid:string;iat:number;exp:number};

export function sessionKey():Buffer {
  const path=process.env.DASHBOARD_SESSION_KEY_FILE||'/host-secrets/dashboard-session-key';
  // Drop trailing newlines as dashboard-entrypoint.sh does, so the proxy and server sign with the same key.
  const raw=fs.readFileSync(/*turbopackIgnore: true*/ path);
  let end=raw.length;
  while(end>0&&raw[end-1]===0x0a)end--;
  const key=raw.subarray(0,end);
  if(key.length<32)throw new Error('Dashboard session key must contain at least 32 bytes.');
  return key;
}

export function issueSession(sub:string,now=Math.floor(Date.now()/1000),key=sessionKey()):string {
  const claims:SessionClaims={sub,kind:'user',sid:crypto.randomUUID(),iat:now,exp:now+SESSION_TTL_SECONDS};
  const payload=Buffer.from(JSON.stringify(claims)).toString('base64url');
  const signature=crypto.createHmac('sha256',key).update(payload).digest('base64url');
  return `${payload}.${signature}`;
}

export function verifySession(value:string|undefined,now=Math.floor(Date.now()/1000),key=sessionKey()):SessionClaims|null {
  if(!value)return null;
  const [payload,signature,...extra]=value.split('.');
  if(!payload||!signature||extra.length)return null;
  const expected=crypto.createHmac('sha256',key).update(payload).digest();
  let supplied:Buffer;
  try{supplied=Buffer.from(signature,'base64url')}catch{return null}
  if(supplied.length!==expected.length||!crypto.timingSafeEqual(supplied,expected))return null;
  try {
    const claims=JSON.parse(Buffer.from(payload,'base64url').toString()) as SessionClaims;
    if(claims.kind!=='user'||typeof claims.sub!=='string'||typeof claims.sid!=='string'||
      !Number.isInteger(claims.exp)||claims.exp<=now||!Number.isInteger(claims.iat)||claims.iat>now) return null;
    return claims;
  } catch { return null }
}
