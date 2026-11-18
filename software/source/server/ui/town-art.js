/* Field Notes cutaway art. Geometry always comes from the loaded simulation. */
(() => {
  const ns = 'http://www.w3.org/2000/svg';
  const cell = 32;
  function node(name, attrs, parent) {
    const element = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    if (parent) parent.append(element);
    return element;
  }
  const rect = (p,x,y,w,h,fill) => node('rect',{x,y,width:w,height:h,fill},p);
  const line = (p,x1,y1,x2,y2,stroke,width) => node('line',{x1,y1,x2,y2,stroke,'stroke-width':width},p);
  const themes = {
    office:['#c5d3ad','#6a8d78'], collaboration:['#d5d2b2','#858568'], library:['#c4c1d8','#7a7699'],
    kitchen:['#ead4a7','#a47b54'], bedroom:['#d9c9df','#8e7498'], hall:['#c1d5d6','#628693'],
    park:['#9ebb7e','#648c57'], street:['#c7bdac','#8b8075']
  };
  function texture(svg,id,w,h,base,details) {
    const defs = svg.querySelector('defs');
    const pattern = node('pattern',{id,width:w,height:h,patternUnits:'userSpaceOnUse'},defs);
    rect(pattern,0,0,w,h,base);
    for (const [x,y,width,height,color] of details) rect(pattern,x,y,width,height,color);
    return `url(#${id})`;
  }
  function tree(layer,x,y) {
    const trunk = node('g',{transform:`translate(${x} ${y})`},layer);
    rect(trunk,2,25,28,5,'#527550'); rect(trunk,13,18,6,10,'#795a42');
    rect(trunk,4,7,24,15,'#477b56'); rect(trunk,8,3,16,22,'#477b56');
    rect(trunk,7,7,18,12,'#72a06a'); rect(trunk,10,8,10,3,'#a4ba75');
    rect(trunk,18,16,8,5,'#5b8c58');
  }
  function fixture(layer, x, y, kind, solid) {
    const p = node('g',{transform:`translate(${x*cell} ${y*cell})`,'data-furniture':kind,
      'data-cell':`${x},${y}`,'data-solid':String(solid)},layer);
    const draw = (x,y,w,h,fill) => rect(p,x,y,w,h,fill);
    const title = node('title',{},p); title.textContent = kind.replaceAll('_',' ');
    if (kind === 'desk') {
      draw(3,9,26,14,'#ba9566'); draw(3,20,26,3,'#8c6447');
      draw(11,4,11,8,'#466962'); draw(12,5,9,5,'#a6c6a4'); draw(12,25,9,5,'#618478');
    } else if (kind === 'bed') {
      draw(4,4,24,24,'#89664c'); draw(6,6,20,19,'#ecdfc6');
      draw(8,8,15,5,'#fcf6e2'); draw(6,15,20,10,'#869ab3');
    } else if (kind === 'chair') {
      draw(9,12,14,15,'#866847'); draw(10,14,12,9,'#b59264');
    } else if (kind === 'plant') {
      draw(10,13,12,13,'#9f6951'); draw(8,7,16,10,'#477957'); draw(13,4,7,10,'#78a163');
    } else if (kind === 'car') {
      draw(2,8,28,16,'#536479'); draw(7,10,18,12,'#95b3ad'); draw(3,10,4,4,'#e6d0a1'); draw(26,18,3,4,'#ce8672');
    } else {
      draw(4,4,24,24,'#83684f'); draw(6,5,20,16,'#bda079'); draw(6,22,20,3,'#5f5344');
      if (/shelf|archive/.test(kind)) {
        draw(7,8,4,10,'#6b8166'); draw(12,10,3,8,'#b26e58'); draw(16,7,4,11,'#668e9a'); draw(21,11,4,7,'#d2b570');
      } else {
        draw(8,8,16,7,'#d9bf98'); draw(15,18,7,3,'#6b806e');
      }
    }
  }
  function geometry(svg, data) {
    svg.replaceChildren();
    node('defs',{},svg);
    const grass = texture(svg,'town-grass',29,21,'#94b57e',[[3,4,2,2,'#7ea36f'],[19,8,2,3,'#87aa72'],[9,17,3,1,'#afc88d']]);
    const paving = texture(svg,'town-paving',32,16,'#d9c394',[[0,7,32,1,'#c6ab7e'],[0,15,32,1,'#c6ab7e'],[16,0,1,7,'#c6ab7e'],[3,8,1,7,'#c6ab7e']]);
    const walk = new Set(data.walkable.map(([x,y]) => `${x},${y}`));
    if (!walk.size) return null;
    const xs = data.walkable.map(([x])=>x), ys = data.walkable.map(([,y])=>y);
    const bounds = [(Math.min(...xs)-2)*cell,(Math.min(...ys)-2)*cell,
      (Math.max(...xs)-Math.min(...xs)+5)*cell,(Math.max(...ys)-Math.min(...ys)+5)*cell];
    rect(svg,...bounds,grass);
    const ground = node('g',{},svg);
    const areas = data.locations.filter(room=>Array.isArray(room.rect));
    const floors = new Map();
    for (const room of areas) {
      const [x0,y0,x1,y1] = room.rect;
      for (let x=x0;x<=x1;x++) for (let y=y0;y<=y1;y++) floors.set(`${x},${y}`,room);
    }
    for (let x=bounds[0]/cell;x<(bounds[0]+bounds[2])/cell;x++) {
      for (let y=bounds[1]/cell;y<(bounds[1]+bounds[3])/cell;y++) {
        const key = `${x},${y}`;
        if (!walk.has(key) && !floors.has(key) && (x*19+y*13)%17 === 0) tree(ground,x*cell,y*cell);
      }
    }
    for (const [x,y] of data.walkable) {
      const key = `${x},${y}`;
      const room = floors.get(key);
      const theme = room ? themes[room.kind] || themes.office : null;
      const group = node('g',{'data-walkable':key},ground);
      rect(group,x*cell,y*cell,cell,cell,theme ? theme[0] : paving);
      if (theme) {
        line(group,x*cell,y*cell+31,x*cell+32,y*cell+31,theme[1],.7);
        line(group,x*cell+16,y*cell+16,x*cell+16,y*cell+31,theme[1],.6);
        if (room.kind !== 'park' && room.kind !== 'street') {
          const [px,py] = [x*cell,y*cell];
          const walls = [[0,-1,px,py,px+32,py],[-1,0,px,py,px,py+32],
            [1,0,px+32,py,px+32,py+32],[0,1,px,py+32,px+32,py+32]];
          for (const [dx,dy,x1,y1,x2,y2] of walls) {
            const neighbor = `${x+dx},${y+dy}`;
            if (!walk.has(neighbor) && !floors.has(neighbor)) line(group,x1,y1,x2,y2,theme[1],5);
          }
        }
      }
    }
    const furniture = node('g',{},svg);
    for (const room of areas) {
      const [x,y] = room.rect;
      const title = node('text',{x:x*cell+5,y:y*cell+11,class:'fn-town-room-name'},svg);
      title.textContent = room.name.toUpperCase();
    }
    for (const item of data.fixtures) {
      if (!walk.has(`${item.x},${item.y}`)) fixture(furniture,item.x,item.y,item.kind,true);
    }
    for (const loc of data.locations) {
      if (['desk','bed','car'].includes(loc.kind)) {
        fixture(furniture,loc.x,loc.y,loc.kind,!walk.has(`${loc.x},${loc.y}`));
      }
      if (loc.kind === 'kitchen') {
        for (const [x,y] of loc.interaction_cells || []) if (walk.has(`${x},${y}`)) fixture(furniture,x,y,'chair',false);
      }
      if (loc.id === 'front' && walk.has(`${loc.x},${loc.y}`)) {
        const door = node('g',{'data-door':`${loc.x},${loc.y}`},furniture);
        rect(door,loc.x*cell,loc.y*cell+5,32,6,'#8c7052');
        rect(door,loc.x*cell+3,loc.y*cell+5,26,3,'#e4cc9d');
      }
    }
    return {bounds, actors:node('g',{'class':'fn-town-actors'},svg)};
  }
  function avatar(parent,key,direction) {
    let hash = 2166136261;
    for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0),16777619) >>> 0;
    const coat = ['#608456','#aa6b52','#657bab','#9677a0'][(hash>>>3)%4];
    const skin = ['#f2c89a','#dca67b','#c48764','#e8b591'][(hash>>>5)%4];
    const hair = ['#433731','#724e39','#4b4152','#98754e'][hash%4];
    const g = node('g',{transform:direction==='left'?'translate(24 0) scale(-1 1)':'',class:'fn-town-avatar'},parent);
    rect(g,3,23,18,3,'#324c3e55'); rect(g,6,19,4,5,'#354a42'); rect(g,14,19,4,5,'#354a42');
    rect(g,4,13,16,8,coat); rect(g,6,4,12,11,hair); rect(g,5,5,14,4,hair);
    if (direction !== 'up') {
      rect(g,direction==='down'?8:11,9,direction==='down'?8:7,6,skin);
      rect(g,14,10,1,1,'#28302c');
      if (direction==='down') rect(g,9,10,1,1,'#28302c');
      rect(g,10,15,5,2,'#e6d4a2');
    } else rect(g,7,16,10,2,'#2d5548');
    if (hash%2) { rect(g,4,5,16,2,coat); rect(g,7,2,10,3,coat); }
    return g;
  }
  window.FieldTownArt = {node,geometry,avatar,cell};
})();
