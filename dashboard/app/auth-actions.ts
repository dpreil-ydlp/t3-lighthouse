'use server';

import {redirect} from 'next/navigation';
import {clearSession,currentSession,setSession} from '../lib/session';
import {apiBase} from '../lib/api';

export type LoginState={error?:string};

export async function login(_state:LoginState,form:FormData):Promise<LoginState>{
  const token=String(form.get('token')||'').trim();
  if(!token)return {error:'Enter the owner token.'};
  try {
    const response=await fetch(`${apiBase}/api/v1/me`,{
      headers:{Authorization:`Bearer ${token}`},cache:'no-store',
    });
    if(!response.ok)return {error:'Invalid owner token.'};
    const principal=await response.json();
    if(principal.kind!=='user'||typeof principal.id!=='string')return {error:'Invalid owner token.'};
    await setSession(principal.id);
  } catch { return {error:'Could not verify the owner token.'} }
  redirect('/');
}

export async function logout(){
  if(!await currentSession())throw new Error('Dashboard session required.');
  await clearSession();
  redirect('/login');
}
