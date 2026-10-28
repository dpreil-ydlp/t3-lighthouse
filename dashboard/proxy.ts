import {NextRequest,NextResponse} from 'next/server';
import {SESSION_COOKIE,validEdgeSession} from './lib/session-edge';

export async function proxy(request:NextRequest){
  const value=request.cookies.get(SESSION_COOKIE)?.value;
  if(await validEdgeSession(value))return NextResponse.next();
  if(request.method==='POST'||request.headers.has('Next-Action'))return new NextResponse('Unauthorized',{status:401});
  const login=new URL('/login',request.url);
  return NextResponse.redirect(login);
}

export const config={matcher:['/((?!login$|healthz$|_next/static|_next/image|favicon.ico|icon.svg$).*)']};
