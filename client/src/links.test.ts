import { describe, expect, it } from 'vitest';
import { linkHref } from './links';

const MAC = '192.168.2.29';

describe('linkHref', () => {
  it('points a loopback address at the Mac the page came from', () => {
    expect(linkHref('http://localhost:3000/review?x=1#top', MAC)).toBe(`http://${MAC}:3000/review?x=1#top`);
    expect(linkHref('http://127.0.0.1:7292/', MAC)).toBe(`http://${MAC}:7292/`);
    expect(linkHref('http://0.0.0.0:8080', MAC)).toBe(`http://${MAC}:8080/`);
    expect(linkHref('http://[::1]:5173/', MAC)).toBe(`http://${MAC}:5173/`);
  });

  it('leaves every other address alone', () => {
    expect(linkHref('https://example.com/a?b=c', MAC)).toBe('https://example.com/a?b=c');
    expect(linkHref('http://localhost.example.com/', MAC)).toBe('http://localhost.example.com/');
    expect(linkHref('http://10.0.0.5:3000/', MAC)).toBe('http://10.0.0.5:3000/');
  });

  it('passes through anything that is not a parseable URL', () => {
    expect(linkHref('not a url', MAC)).toBe('not a url');
  });
});
