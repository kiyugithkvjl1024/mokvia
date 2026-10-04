const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const target = path.join(__dirname, '../webapp/static/local-capture.js');
assert.ok(fs.existsSync(target), 'capture source UI is not implemented');
const {captureMetadata} = require(target);
const source = {kind:'outlook', url:'https://outlook.office.com/mail/id/synthetic', scope_id:'synthetic',message_id:'one'};
const metadata = {version:1,capture_id:'11111111-1111-4111-8111-111111111111',accepted_at:'2026-10-03T23:00:00Z',destination:'today',source};
const body = value => '<!-- mokvia-capture-v1 '+JSON.stringify(value)+' -->\n';
assert.equal(captureMetadata(body(metadata)).source.url, source.url);
// Store serializes a blank line after YAML frontmatter; entity.body retains it.
assert.equal(captureMetadata('\n'+body(metadata)).source.url, source.url);
assert.equal(captureMetadata('\r\n'+body(metadata)).source.url, source.url);
for(const url of ['javascript:alert(1)','https://attacker.example',source.url.replace('.com/','.com.attacker.example/'),source.url.replace('//','//user@'),source.url.replace('.com/','.com:999/')]) {
  assert.equal(captureMetadata(body({...metadata,source:{...source,url}})),null);
}
for(const invalid of ['',body({...metadata,version:2}),'<p>source</p>', '<!-- mokvia-capture-v1 { -->']) assert.equal(captureMetadata(invalid),null);
console.log('Capture metadata source-link checks passed');
