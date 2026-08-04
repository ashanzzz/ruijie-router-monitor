const GibberishAES = require('./aes.js');

const Zt = GibberishAES.dec("U2FsdGVkX19ecPYL/ZAlcSG29wb6ivqD9YjEM30k1h8=","eweb").replace(/\s+/g,"");
const password = process.argv[2] || "a123456789.";
const encPass = GibberishAES.enc(password, Zt).replace(/\s/g,"");

console.log(encPass);
