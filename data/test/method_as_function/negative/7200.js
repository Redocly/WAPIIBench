// Test trailing string concatenation with extra final quotation mark
const axios = require('axios');
const name = 'johndoe'
axios.get('https://petstore.swagger.io/v2/customers/' + name');