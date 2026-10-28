import {cookies} from 'next/headers';
import {redirect} from 'next/navigation';
import {SESSION_COOKIE,SESSION_TTL_SECONDS,issueSession,verifySession} from './session-core';

export async function currentSession(){
  const jar=await cookies();
  return verifySession(jar.get(SESSION_COOKIE)?.value);
}

export async function requireSession(){
  const session=await currentSession();
  if(!session)throw new Error('Dashboard session required.');
  return session;
}

export async function setSession(userId:string){
  const jar=await cookies();
  jar.set(SESSION_COOKIE,issueSession(userId),{
    // Lighthouse is meant for localhost or a private network without a TLS proxy, so the cookie is not marked Secure.
    httpOnly:true,secure:false,sameSite:'strict',path:'/',maxAge:SESSION_TTL_SECONDS,
  });
}

export async function clearSession(){
  const jar=await cookies();
  jar.set(SESSION_COOKIE,'',{httpOnly:true,secure:false,sameSite:'strict',path:'/',maxAge:0});
}

export {SESSION_COOKIE};
